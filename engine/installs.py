"""v0.3.0 R4: one-click install of the local R2T2 runtime (CPU / CUDA).

Owner rulings (2026-11-16), all binding:

* TWO manual buttons, not auto-detection: 「安装 CPU 引擎」/「安装 CUDA
  引擎」.  N-card users install both — the CUDA pack only carries the
  GPU accel DLLs, the worker and embedded python live in the CPU pack,
  so a CUDA install before a CPU one is refused here with a hint.
* downloads land ONLY in the user's own temp dir as set in their
  environment (TEMP/TMP), inside one fixed subdir
  ``video-xray-install/<variant>/``.  We never invent random temp dirs
  and never use the Hermes scratch dir; the fixed name lets an
  interrupted .part resume across gateway restarts.
* iron rule: once the pack is installed the zip AND the temp subdir are
  deleted — the user's disk keeps only the extracted runtime.  On
  cancel/failure the .part stays on purpose, so the next click resumes.
* the extract target is ``<HERMES_HOME>/asr-r2t2/`` — exactly the
  default engine root audio.py already launches (``audio._ASR_ROOT``),
  so after a successful install nothing else needs registering.
* single-flight lock: while either install runs the other start is
  refused backend-side too (also while a weights download runs; the two
  share one cancel event — the R3 kernel checks it per chunk).

Packs ship as GitHub release assets of this repo
(``r2t2-runtime-cpu-windows-x64.zip`` / ``r2t2-runtime-cuda-windows-x64.zip`` under the
tag v0.3.0 — publishing MUST keep that tag).  URLs are static
release-download paths: asset *ids* churn on re-upload, names and tag
urls do not, and no API call/auth quirk stands between a first-time
user and their bytes.  A gh-proxy mirror leg is tried second for
domestic users without a proxy.  ``VA_XRAY_INSTALL_URL_CPU`` /
``VA_XRAY_INSTALL_URL_CUDA`` override the URL for experiments.

The byte transfer reuses the proven R3 kernel ``downloads._fetch_one``
(Range-resume, sha256 verify, auto source switch) — do not fork it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from typing import Any, Dict, Optional

from . import audio, downloads
from .jobs import PLUGIN_DATA
from .llm import ui
from .logbox import get_logger, log_file

log = get_logger()

# release tag the packs live under.  Asset names are deliberately
# VERSION-FREE (owner ruling 2026-11-17): the runtime packs are generic —
# a plugin upgrade that reuses the same runtime must not force users to
# re-download a 500 MB pack under a new name.  The tag inside _DL_BASE
# carries the version; bump it only when the packs themselves move.
_DL_BASE = "https://github.com/LuuMoss/video-xray/releases/download/v0.3.0/"
# second leg for users whose direct GitHub link is slow/dead (no key, no
# quota; worst case it fails the same way and the error is the same)
_MIRROR_PREFIX = "https://gh-proxy.com/"

# "need": files that must exist under the engine root after extraction —
# spot-checked instead of trusting the zip unpacked cleanly.
VARIANTS: Dict[str, Dict[str, Any]] = {
    "cpu": {
        "asset": "r2t2-runtime-cpu-windows-x64.zip",
        "size": 50871157,  # exact GitHub asset size (verified 2026-11-16)
        "sha": "4eef1d9cbce98105b8b0db883a88fdd8b64d31846a48901e77b6290fd5e1fa9e",
        "need": ("worker.py",
                 "core/confucius/native.py",
                 "runtime/python/python.exe",
                 "runtime/native-cpu/lib/ggml.dll"),
    },
    "cuda": {
        "asset": "r2t2-runtime-cuda-windows-x64.zip",
        "size": 510079950,
        "sha": "77ca1ee266313f8d2536ec9208f5f58eb1f43f185e02cd002dcb2ba1ee38e4c4",
        "need": ("runtime/native-cuda/python/cublas64_12.dll",
                 "runtime/native-cuda/python/cublasLt64_12.dll",
                 "runtime/native-cuda/python/cudart64_12.dll"),
    },
    # Whisper runtime: self-contained Python 3.14 + faster-whisper 1.2.1
    # (+ctranslate2/av/onnxruntime, the exact set proven on the owner box).
    # ONE pack covers CPU and NVIDIA alike — the worker runs device=auto,
    # ctranslate2 loads CUDA only when the machine has it.  Pack root
    # holds python.exe directly so the panel's manual 「登记本地引擎」
    # folder form recognizes it too (its first candidate is <root>/python.exe).
    "whisper": {
        "asset": "whisper-runtime-windows-x64.zip",
        "size": 111344962,
        "sha": "3d055a27f5befeadbdcbdba407a1117ce7f42a4aa06a8efa31ad69342c138ee5",
        "need": ("python.exe",
                 "Lib/site-packages/faster_whisper/__init__.py",
                 "worker.py"),
    },
}

STATE_FILE = PLUGIN_DATA / "install-task.json"
_FLUSH_INTERVAL = 2.0  # seconds between state-file writes while busy

_LOCK = threading.Lock()
_thread: Optional[threading.Thread] = None
_gpu_cache: Optional[bool] = None


# ---------------------------------------------------------------- state ---
def _save(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _load() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gpu_present() -> bool:
    """One-shot nvidia-smi probe for the panel's gold glow (fail-soft:
    no nvidia-smi / no NVIDIA driver -> False).  Cached forever."""
    global _gpu_cache
    if _gpu_cache is None:
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                               timeout=5)
            _gpu_cache = r.returncode == 0 and b"GPU" in r.stdout
        except Exception:
            _gpu_cache = False
    return _gpu_cache


def _root_for(variant: str) -> str:
    """Extract/install root per variant.  R2T2 packs merge into the
    default engine root audio.py already launches; the Whisper runtime
    lives in its own sibling dir <HERMES_HOME>/asr-whisper."""
    if variant == "whisper":
        return os.path.join(os.path.dirname(audio._ASR_ROOT), "asr-whisper")
    return audio._ASR_ROOT


def _installed(variant: str) -> bool:
    root = _root_for(variant)
    return all(os.path.isfile(os.path.join(root, *p.split("/")))
               for p in VARIANTS[variant]["need"])


def installed_status() -> Dict[str, bool]:
    """Live on-disk flags (true on pre-installed machines too, not just
    after OUR installs — the panel labels buttons from this)."""
    return {v: _installed(v) for v in VARIANTS}


def _urls(variant: str) -> list:
    override = (os.environ.get("VA_XRAY_INSTALL_URL_"
                               + variant.upper()) or "").strip()
    url = override or (_DL_BASE + VARIANTS[variant]["asset"])
    if url.startswith("http"):
        return [url, _MIRROR_PREFIX + url]
    return [url]  # local path (experiments); kernel accepts paths too


# ------------------------------------------------------------------ run ---
def _run(variant: str) -> None:
    cfg = VARIANTS[variant]
    st = _load()
    last = [0.0]

    def flush(force: bool = False) -> None:
        now = time.time()
        if force or now - last[0] >= _FLUSH_INTERVAL:
            last[0] = now
            _save(st)

    try:
        # owner iron rule: the user's OWN temp dir (TEMP/TMP env), never
        # a dir we invent elsewhere, never the Hermes scratch
        tmpbase = (os.environ.get("TEMP") or os.environ.get("TMP")
                   or tempfile.gettempdir())
        work = os.path.join(tmpbase, "video-xray-install", variant)
        os.makedirs(work, exist_ok=True)

        st["phase"] = ui("准备下载", "preparing download")
        st["percent"] = 1
        flush(True)

        urls = _urls(variant)
        zip_path = os.path.join(work, cfg["asset"])
        part = zip_path + ".part"
        resume = os.path.getsize(part) if os.path.exists(part) else 0
        total = cfg["size"]
        st["files"] = [{"name": cfg["asset"], "bytes_total": total,
                        "done": resume}]

        def _dl_flush(force: bool = False, _f=flush) -> None:
            # percent rides on the real byte counter the kernel updates
            st["percent"] = min(70, (st["files"][0].get("done") or 0) * 70
                                // max(total, 1))
            _f(force)

        st["phase"] = ui("下载引擎压缩包", "downloading engine pack")
        flush(True)

        # R3 kernel: resume, sha256, auto-switch legs, raises
        # KeyboardInterrupt on cancel, ConnectionError when all legs die
        downloads._fetch_one(urls, zip_path, total, cfg["sha"], st, 0,
                             lambda: _dl_flush())
        if downloads._cancel.is_set():
            raise KeyboardInterrupt

        # ---- extract straight into the engine root (merge/overwrite) ----
        if downloads._cancel.is_set():
            raise KeyboardInterrupt
        st["phase"] = ui("解压引擎文件", "extracting engine files")
        st["percent"] = 70
        flush(True)
        root = _root_for(variant)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                x_total = sum(i.file_size for i in infos) or 1
                x_done = 0
                for info in infos:
                    if downloads._cancel.is_set():
                        raise KeyboardInterrupt
                    target = os.path.join(root,
                                          info.filename.replace("/", os.sep))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        while True:
                            buf = src.read(4 * 1024 * 1024)
                            if not buf:
                                break
                            dst.write(buf)
                            x_done += len(buf)
                            st["percent"] = 70 + min(22, x_done * 22
                                                     // x_total)
                            flush()
        except PermissionError:
            st["error_code"] = "files_busy"
            st["error"] = ui("文件被占用（引擎可能正在转录），请稍后重试安装",
                             "files are busy (a transcription may be "
                             "running) — try the install again shortly")
            st["state"] = "failed"
            return

        # ---- verify key files, then the iron-rule cleanup ----
        st["phase"] = ui("核对关键文件", "verifying files")
        st["percent"] = 93
        flush(True)
        missing = [p for p in cfg["need"]
                   if not os.path.isfile(os.path.join(root, *p.split("/")))]
        if missing:
            st["error_code"] = "files_missing"
            st["error_detail"] = ", ".join(missing)
            st["error"] = ui("解压后缺少关键文件：" + "、".join(missing),
                             "key files missing after extraction: "
                             + ", ".join(missing))
            st["state"] = "failed"
            return

        st["phase"] = ui("清理安装包", "cleaning up the archive")
        os.remove(zip_path)                       # zip gone
        shutil.rmtree(work, ignore_errors=True)  # temp subdir gone

        # Whisper gets one extra step the owner asked for: auto-register
        # the freshly installed runtime as the local engine (same
        # two-stage verify the manual 「登记本地引擎」 form runs, so a
        # box with cached/absent weights behaves identically either way).
        # Failure is not fatal — the runtime is installed; the panel's
        # grayed-out guide + manual register stay available.
        if variant == "whisper":
            st["phase"] = ui("注册引擎", "registering engine")
            st["percent"] = 96
            flush(True)
            try:
                reg = audio.register_asr_engine(root)
            except Exception as exc:  # noqa: BLE001
                reg = {"ok": False, "reason": str(exc)}
            if reg.get("ok"):
                st["detail_code"] = "registered"
                st["detail"] = ui("已自动登记为本地转录引擎",
                                  "auto-registered as the local engine")
            else:
                st["detail_code"] = "reg_incomplete"
                st["reg_reason"] = str(reg.get("reason") or "")[:160]
                st["detail"] = ui(
                    "运行库已装好，但自动注册未完成（"
                    + str(reg.get("reason") or "未知原因")
                    + "）——下载模型权重后可在面板点「登记本地引擎」选择该目录",
                    "runtime installed, but auto-registration did not "
                    "complete (" + str(reg.get("reason") or "unknown")
                    + ") — download the weights, then use 「登记本地引擎」 "
                    "and pick this folder")
        st["percent"] = 100
        st["phase"] = ui("安装完成", "installed")
        st["state"] = "done"
        st["error"] = None
    except KeyboardInterrupt:
        st["state"] = st["error_code"] = "cancelled"
        st["error"] = ui("已取消；已下载的字节会续传，再点一次安装即可",
                         "cancelled — partial bytes resume on the next "
                         "install click")
    except Exception as exc:  # noqa: BLE001 — code lets the panel localize
        log.warning("install: _run crash variant=%s", variant, exc_info=True)
        st["state"] = "failed"
        st["error"] = f"{type(exc).__name__}: {exc}"
        st["error_code"] = ("cancelled" if _cancel_event.is_set()
                            else str(getattr(exc, "code", "") or "internal_error"))
        st["error_detail"] = str(exc)[:300]
    finally:
        st["finished_at"] = time.time()
        _save(st)
        el = st["finished_at"] - (st.get("started_at") or st["finished_at"])
        if st["state"] == "done":
            log.info("install: DONE variant=%s in %.0fs", variant, el)
        elif st["state"] == "cancelled":
            log.info("install: CANCELLED variant=%s after %.0fs", variant, el)
        else:
            log.error("install: FAILED variant=%s code=%s detail=%.200s",
                      variant, st.get("error_code"),
                      str(st.get("error_detail") or st.get("error") or ""))


# ---------------------------------------------------------------- api -----
def status() -> Dict[str, Any]:
    st = _load()
    if st.get("state") == "running" and not (_thread and _thread.is_alive()):
        st["state"] = "failed"
        st["error_code"] = "interrupted_restart"
        st["error"] = ui("网关重启打断了安装——再点一次即可续传",
                         "gateway restart interrupted the install — click "
                         "again to resume")
    out = dict(st)
    out["log_file"] = log_file()
    files = st.get("files") or []
    done = sum(f.get("done", 0) for f in files)
    out["bytes_done"] = done
    try:
        secs = max(time.time() - float(st.get("started_at") or time.time()), .01)
        rate = done / secs
    except (TypeError, ValueError):
        rate = 0.0
    if st.get("state") == "running" and rate > 0:
        out["rate_Bps"] = round(rate, 1)
    return out


def _rej(r: Dict[str, Any]) -> Dict[str, Any]:
    """Log a start() rejection and pass the response through untouched."""
    log.warning("install: rejected %s (%.200s)", r.get("reason_code"),
                str(r.get("reason_detail") or r.get("reason") or ""))
    return r


def start(variant: str) -> Dict[str, Any]:
    """Begin one runtime install (variant cpu|cuda).  Returns {ok} or
    {ok:False, reason}; the panel polls status()."""
    global _thread
    variant = (variant or "").strip().lower()
    if variant not in VARIANTS:
        return _rej({"ok": False, "reason_code": "bad_variant",
                "reason_detail": variant,
                "reason": ui("未知引擎类型", "unknown engine variant")})
    with _LOCK:
        st = _load()
        if st.get("state") == "running" and _thread and _thread.is_alive():
            return _rej({"ok": False, "reason_code": "busy", "reason": ui(
                "已有一个引擎安装在跑——请先取消或等它完成",
                "an engine install is already running — cancel it or "
                "wait for it to finish")})
        if downloads.status().get("state") == "running":
            return _rej({"ok": False, "reason_code": "dl_busy", "reason": ui(
                "已有权重下载在跑——请先取消或等它完成（下载与安装互斥）",
                "a weights download is running — cancel it or wait "
                "(downloads and installs are mutually exclusive)")})
        if variant == "cuda" and not _installed("cpu"):
            return _rej({"ok": False, "reason_code": "cuda_first", "reason": ui(
                "请先安装 CPU 引擎库——CUDA 包只含 GPU 加速件，"
                "worker 与嵌入式 Python 都在 CPU 包里",
                "install the CPU engine pack first — the CUDA pack only "
                "carries GPU accel files; the worker and embedded python "
                "come from the CPU pack")})
        st = {"variant": variant, "state": "running", "phase": "",
              "files": [], "percent": 0, "error": None,
              "error_code": None, "error_detail": None,
              "started_at": time.time()}
        _save(st)
        downloads._cancel.clear()
        _thread = threading.Thread(target=_run, args=(variant,),
                                   name="vx-install", daemon=True)
        _thread.start()
        log.info("install: start variant=%s", variant)
        return {"ok": True, **status()}


def cancel() -> Dict[str, Any]:
    """Stop the running install; the .part stays in the temp subdir so
    the next click resumes where it stopped."""
    downloads._cancel.set()  # the R3 kernel checks this per chunk
    st = _load()
    if st.get("state") != "running":
        return {"ok": True, "state": st.get("state") or "idle"}
    return {"ok": True, "cancelling": True, **status()}
