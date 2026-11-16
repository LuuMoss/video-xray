"""v0.3.0 download wizard backend: R2T2 weights + whisper sizes.

Sources are OFFICIAL ONLY (owner 2026-11-16: never t8mars' own GitHub
links) and every domestic/overseas combination must work (owner:
domestic users without a proxy are the ones who need this):

  | file set                   | modelscope.cn          | huggingface.co | hf-mirror.com |
  |----------------------------|------------------------|----------------|---------------|
  | R2T2 gguf + mmproj         | official NetEase repo, | same bytes     | mirrors it    |
  |                            | byte-identical (proven | (overseas)     |               |
  |                            | via matching SHA256)   |                |               |
  | FireRedVAD ONNX + cmvn.ark | absent (only pth)      | ONLY source    | mirrors it    |
  | faster-whisper (Systran)   | absent                 | official       | mirrors it    |

Per-file URLs carry every legal source for that file, ordered by a
live concurrent TLS-handshake probe (fastest wins; never cached, so a
VPN toggle re-routes on the next start — this machine proved
huggingface.co is dead without WARP while modelscope.cn's domestic CDN
is the fast lane for the big ggufs). Cross-source resume works because
every source serves the same bytes at the same offsets (.part files).
LFS SHA256 (from API metadata, identical across sources) is verified
before a file is accepted — a corrupt download is refused, never
installed.

Plain urllib streaming with Range-resume, one task slot at a time (the
panel gates the buttons — owner rule: no popups, every network op shows
a progress bar). Progress persists as JSON under plugin-data so the
front end can poll and a gateway restart does not lose the picture
(v0.3.0 acceptance: kill the gateway mid-download, reopen the panel,
the progress survives and one click resumes). Whisper files land in the
HF hub cache layout the worker already scans:
<target>/models--Systran--faster-whisper-<size>/snapshots/<sha>/.
"""
import hashlib
import json
import os
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from .llm import ui
from .logbox import get_logger, log_file

log = get_logger()
from .jobs import PLUGIN_DATA

MS_REPO = "netease-youdao/Confucius4-R2T2-GGUF"      # official NetEase Youdao
VAD_REPO = "t8star/Confucius-R2t2-Comfy"             # official Comfy mirror:
#   the ONLY place the Apache-2.0 FireRedVAD ONNX export is published
VAD_PREFIX = "FireRedVAD-ONNX/"
WHISPER_REPO = "Systran/faster-whisper-{size}"
R2T2_VARIANT = {                                     # audio model selection
    "q8": ("Confucius4-R2T2-Q8_0.gguf", "mmproj-Confucius4-R2T2-Q8_0.gguf"),
    "q4": ("Confucius4-R2T2-Q4_K_M.gguf", "mmproj-Confucius4-R2T2-Q8_0.gguf"),
}
MS_HOST = "modelscope.cn"
HF_HOSTS = ("hf-mirror.com", "huggingface.co")
_URL = {MS_HOST: "https://modelscope.cn",
        HF_HOSTS[0]: "https://hf-mirror.com",
        HF_HOSTS[1]: "https://huggingface.co"}
# whisper snapshots: only these files are what faster-whisper actually loads
_WHISPER_FILES = ("model.bin", "config.json", "tokenizer.json",
                  "vocabulary.json", "preprocessor_config.json",
                  "normalizer_config.json", "tokenizer_config.json")
_TIMEOUT = 60          # socket timeout: a slow domestic hf-mirror leg stalls
                       # for many seconds between 1 MB chunks; 15 s false-kills
_CHUNK = 1 * 1024 * 1024   # stream granularity: keeps cancel latency ~seconds
_PROBE_S = 6.0             # one TLS handshake sample budget
_FLUSH_INTERVAL = 2.0      # progress JSON write cap (seconds)
STATE_FILE = PLUGIN_DATA / "download-task.json"

_task_lock = threading.Lock()
_thread = None
_cancel = threading.Event()


# ---- state ---------------------------------------------------------------
def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    PLUGIN_DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def status() -> dict:
    """Panel projection: state, per-file bytes, overall rate/ETA."""
    st = _load()
    if st.get("state") == "running":
        # a gateway restart kills the worker thread mid-flight; say so
        if _thread is None or not _thread.is_alive():
            if not st.get("done_bytes_seen"):
                st["state"] = "interrupted"
                st["error_code"] = "interrupted_restart"
                st["error"] = ui("下载被网关重启打断，可重新开始（已下部分会续传）",
                                 "interrupted by a gateway restart; start it "
                                 "again — partial bytes resume")
            else:
                st["state"] = "done"
            _save(st)
    files = st.get("files") or []
    done = sum(f["done"] for f in files)
    total = sum(f["total"] for f in files)
    out = {k: st.get(k) for k in
           ("kind", "size", "target", "state", "error", "error_code",
            "error_detail", "sources")}
    out.update(files=[{k: f[k] for k in ("path", "done", "total")}
                      for f in files],
               done_bytes=done, total_bytes=total,
               percent=round(100.0 * done / total, 1) if total else 0.0)
    if st.get("t0") and done and st.get("state") == "running":
        dt = time.time() - st["t0"]
        rate = done / dt if dt > 0 else 0.0
        out["rate_bps"] = int(rate)
        out["eta_s"] = int((total - done) / rate) if rate > 0 else None
    out["log_file"] = log_file()
    return out


# ---- live source probe (never cached: a VPN toggle must re-route) -------
def _handshake(host: str) -> float:
    t = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=_PROBE_S) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                pass
        return (time.perf_counter() - t) * 1000.0
    except Exception:                                # noqa: BLE001
        return float("inf")


def _probe() -> list:
    """[host...] ordered by concurrent TLS latency (min of 2 samples each);
    unreachable hosts drop to the tail in legacy priority order."""
    hosts = (MS_HOST,) + HF_HOSTS
    with ThreadPoolExecutor(max_workers=len(hosts) * 2) as ex:
        futs = {h: [ex.submit(_handshake, h) for _ in range(2)] for h in hosts}
        best = {h: min(f.result() for f in fs) for h, fs in futs.items()}
    reachable = sorted((h for h in hosts if best[h] < float("inf")),
                       key=lambda h: best[h])
    tail = [h for h in (MS_HOST, HF_HOSTS[0], HF_HOSTS[1])
            if h not in reachable]
    return reachable + tail


# ---- metadata ------------------------------------------------------------
def _get_json(url: str):
    req = urllib.request.Request(url,
                                 headers={"User-Agent": "video-xray/0.3.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _hf_tree(repo: str, order):
    """(tree, host) from the closest reachable HF host. Two rounds because
    hf-mirror shares one global Hub API quota — a live 429 here is
    transient, not the mirror being dead."""
    last = None
    for rnd in range(2):
        for h in order:
            if h not in HF_HOSTS:
                continue
            try:
                return _get_json(
                    f"{_URL[h]}/api/models/{repo}/tree/main?recursive=true"), h
            except Exception as exc:                 # noqa: BLE001
                last = exc
        if rnd == 0:
            time.sleep(3.0)
    exc = last or ConnectionError("every source unreachable")
    exc.code = "no_source_reachable"
    raise exc


def _ms_meta():
    """official ModelScope repo -> {path: (bytes, sha256|None)} or None."""
    try:
        j = _get_json(f"{_URL[MS_HOST]}/api/v1/models/{MS_REPO}/repo/files"
                      f"?Revision=master&Recursive=true")
        return {f["Path"]: (int(f.get("Size") or 0),
                            (f.get("Sha256") or "").lower() or None)
                for f in j.get("Data", {}).get("Files", [])
                if isinstance(f, dict) and f.get("Path")}
    except Exception:                                # noqa: BLE001
        return None


def _urls(repo: str, path: str, order, ms: bool):
    """every legal URL for one file, probe order first (resume-compatible:
    identical bytes at identical offsets on every source)."""
    out = []
    for h in order:
        if h == MS_HOST:
            if ms:
                out.append(f"{_URL[MS_HOST]}/models/{repo}/resolve/master/{path}")
        else:
            out.append(f"{_URL[h]}/{repo}/resolve/main/{path}")
    return out


def _sha(ent: dict):
    return ((ent.get("lfs") or {}).get("oid") or "").lower() or None


def _manifest(kind: str, size: str):
    """(files, probe order). files = [(target-rel path, bytes, [urls],
    sha256|None)]. r2t2 = official NetEase ggufs (variant pair; size+SHA
    from the ModelScope API when reachable, else the HF LFS oid) + the 3
    VAD files from the one repo publishing the ONNX export; whisper =
    official Systran repo laid out HF-hub-cache style under the target."""
    order = _probe()
    if kind == "r2t2":
        want = R2T2_VARIANT.get(size or "q8") or R2T2_VARIANT["q8"]
        meta = _ms_meta()
        tree = None
        if not meta or not all(w in meta for w in want):
            tree, _ = _hf_tree(MS_REPO, order)
        files = []
        for name in want:
            if meta and name in meta:
                total, sha = meta[name]
            else:
                ent = next((e for e in tree if e.get("path") == name), None)
                if not ent:
                    fail(ValueError, "repo_missing_file",
                         f"official repo missing {name}")
                total, sha = int(ent.get("size") or 0), _sha(ent)
            files.append((name, total,
                          _urls(MS_REPO, name, order, ms=True), sha))
        vtree, _vh = _hf_tree(VAD_REPO, order)
        for ent in vtree:
            p = ent.get("path", "")
            if ent.get("type") == "file" and p.startswith(VAD_PREFIX) \
                    and "/.cache/" not in "/" + p:
                files.append((p, int(ent.get("size") or 0),
                              _urls(VAD_REPO, p, order, ms=False), _sha(ent)))
        if not any(p.endswith(".onnx") for p, _, _, _ in files):
            fail(ValueError, "repo_missing_files",
                 "VAD model files not found in " + VAD_REPO)
        return files, order
    repo = WHISPER_REPO.format(size=size)
    tree, _h = _hf_tree(repo, order)
    sha_dir = None
    for h in order:
        if h in HF_HOSTS:
            try:
                sha_dir = str(_get_json(f"{_URL[h]}/api/models/{repo}"
                                        ).get("sha") or "") or None
                break
            except Exception:                        # noqa: BLE001
                continue
    base = (f"models--Systran--faster-whisper-{size}/snapshots/"
            f"{sha_dir or 'main'}/")
    files = []
    for ent in tree:
        if ent.get("type") != "file":
            continue
        p = ent["path"]
        if os.path.basename(p) not in _WHISPER_FILES:
            continue
        files.append((base + os.path.basename(p), int(ent.get("size") or 0),
                      _urls(repo, p, order, ms=False), _sha(ent)))
    if not files:
        fail(ValueError, "repo_missing_file",
             f"no whisper {size} files in the repo")
    return files, order


# ---- download core -------------------------------------------------------
def _verify(part: str, sha) -> None:
    if not sha:                                      # plain small file: the
        return                                       # size check already proved
    h = hashlib.sha256()                             # it byte-exact
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    if h.hexdigest() != sha:                         # wrong bytes: refuse to
        os.remove(part)                              # install (acceptance #4)
        fail(ValueError, "sha_mismatch",
             f"{os.path.basename(part)} SHA256 mismatch — refused")


def _fetch_one(urls, dest: str, total: int, sha, st: dict, idx: int,
               flush) -> None:
    """Stream one file; try each source in probe order. Resume across and
    within sources: the .part byte count is source-independent."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    last = None
    for url in urls:
      for _attempt in range(2):
        if _cancel.is_set():
            raise KeyboardInterrupt()
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have and total and have >= total:
                have = 0                             # stale: restart clean
                os.remove(part)
            hdrs = {"User-Agent": "video-xray/0.3.0",
                    "Range": f"bytes={have}-"} if have else {
                        "User-Agent": "video-xray/0.3.0"}
            req = urllib.request.Request(url, headers=hdrs)
            mode = "ab" if have else "wb"
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                if have and r.status == 200:         # server ignored Range
                    have = 0
                    mode = "wb"
                with open(part, mode) as fh:
                    st["files"][idx]["done"] = have
                    while not _cancel.is_set():
                        buf = r.read(_CHUNK)
                        if not buf:
                            break
                        fh.write(buf)
                        have += len(buf)
                        st["files"][idx]["done"] = have
                        flush()
            if total and have < total:               # truncated -> retry/leg
                raise ConnectionError(
                    f"{os.path.basename(dest)} stream ended early "
                    f"({have}/{total})")
            _verify(part, sha)
            os.replace(part, dest)
            log.info("download: file ok %s (%.1f MB)",
                     name, dest.stat().st_size / 1e6)
            return
        except urllib.error.HTTPError as exc:
            if exc.code in (416,) and total and os.path.exists(part):
                _verify(part, sha)
                os.replace(part, dest)               # range beyond EOF: done
                return
            last = exc                               # 404 etc: this source is
            break                                    # dead for this file -> next
        except ValueError:                           # SHA mismatch: the bytes
            last = "sha256"                          # here are bad; let the
            break                                    # next source refetch clean
        except Exception as exc:                     # noqa: BLE001
            last = exc                               # timeout/stall: SAME leg
            continue                                 # retries with .part resume
    exc = ConnectionError(
        f"{os.path.basename(dest)}: every source failed: {last}")
    exc.code = "all_sources_failed"
    raise exc


def _run(kind: str, target: str, size: str) -> None:
    st = _load()
    try:
        files, order = _manifest(kind, size)
        st.update(files=[{"path": p, "done": 0, "total": t, "urls": u,
                          "sha": s} for p, t, u, s in files],
                  sources=order, t0=time.time(), state="running", error=None,
                  error_code=None, error_detail=None, done_bytes_seen=False)
        _save(st)
        last_flush = [0.0]

        def flush(force: bool = False) -> None:
            now = time.time()
            if force or now - last_flush[0] >= _FLUSH_INTERVAL:
                last_flush[0] = now
                _save(st)

        for i, f in enumerate(st["files"]):
            if _cancel.is_set():
                raise KeyboardInterrupt()
            dest = os.path.join(target, *f["path"].split("/"))
            if os.path.exists(dest) and (not f["total"]
                                         or os.path.getsize(dest) == f["total"]):
                f["done"] = f["total"]               # already in place
                st["done_bytes_seen"] = True
                flush(True)
                continue
            _fetch_one(f["urls"], dest, f["total"], f["sha"], st, i, flush)
            st["done_bytes_seen"] = True
            flush(True)
        st["state"] = "cancelled" if _cancel.is_set() else "done"
        if st["state"] == "cancelled":
            st["error_code"] = "cancelled"
    except KeyboardInterrupt:
        st["state"] = st["error_code"] = "cancelled"
    except Exception as exc:                          # noqa: BLE001 — the code
        st["state"] = "error"                         # lets the panel localize
        st["error"] = str(exc) or exc.__class__.__name__
        st["error_code"] = str(getattr(exc, "code", "") or "internal_error")
        st["error_detail"] = st["error"]
    finally:
        _save(st)
        _st = str(st.get("state"))
        if _st == "done":
            log.info("download: DONE kind=%s files=%d",
                     st.get("kind"), len(st.get("files") or []))
        elif _st == "cancelled":
            log.info("download: CANCELLED kind=%s (.part bytes kept for resume)",
                     st.get("kind"))
        elif _st == "error":
            log.error("download: FAILED kind=%s code=%s detail=%.200s",
                      st.get("kind"), st.get("error_code"),
                      str(st.get("error_detail") or st.get("error") or ""))


# ---- public API (routes call these) --------------------------------------
def defaults() -> dict:
    """Panel prefill: whisper default = the HF hub cache root the worker
    already scans; drives = local drives ordered by free space (the
    wizard's default target = suggested subdir on the roomiest drive)."""
    if os.name == "nt":
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        home = os.path.expanduser("~")
    hub = os.path.join(home, ".cache", "huggingface", "hub")
    drives = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            u = shutil.disk_usage(root)
            drives.append({"root": root, "free_gb": round(u.free / 1e9, 1)})
        except OSError:
            pass
    drives.sort(key=lambda d: -d["free_gb"])
    return {"ok": True, "whisper_hub": hub, "drives": drives}


def _rej(code: str, detail: str = "") -> None:
    """One-line trace for a start() rejection (response itself untouched)."""
    log.warning("download: rejected %s (%.200s)", code, detail or "-")


def start(kind: str, target: str, size: str = "") -> dict:
    global _thread
    kind = str(kind or "").strip().lower()
    if kind not in ("r2t2", "whisper"):
        _rej("bad_kind", f"kind={kind!r}")
        return {"ok": False, "reason_code": "bad_kind",
                "reason": ui("kind 需为 r2t2 或 whisper",
                             "kind must be r2t2 or whisper")}
    size = str(size or "").strip().lower()
    if kind == "whisper":
        from .audio import WHISPER_SIZES
        if size not in WHISPER_SIZES:
            _rej("bad_kind", f"size={size!r}")
            return {"ok": False, "reason_code": "bad_kind",
                    "reason": ui(f"whisper 尺寸需为 {'/'.join(WHISPER_SIZES)}",
                                 f"whisper size must be one of "
                                 f"{'/'.join(WHISPER_SIZES)}")}
    elif kind == "r2t2":
        size = size or "q8"
        if size not in R2T2_VARIANT:
            _rej("bad_kind", f"size={size!r}")
            return {"ok": False, "reason_code": "bad_kind",
                    "reason": ui("r2t2 精度需为 q8 或 q4",
                                 "r2t2 variant must be q8 or q4")}
    target = str(target or "").strip().strip('"')
    if not target:
        _rej("no_target")
        return {"ok": False, "reason_code": "no_target",
                "reason": ui("请先填写下载目录",
                                          "fill in the target directory first")}
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".vx-write-test")
        with open(probe, "wb") as fh:
            fh.write(b"x")
        os.remove(probe)
    except OSError as exc:
        _rej("not_writable", str(exc))
        return {"ok": False, "reason_code": "not_writable",
                "reason_detail": str(exc)[:160],
                "reason": ui(f"目录不可写：{exc}",
                                          f"target dir not writable: {exc}")}
    with _task_lock:
        st = _load()
        if st.get("state") == "running" and _thread and _thread.is_alive():
            _rej("busy", "a download is already running")
            return {"ok": False, "reason_code": "busy",
                    "reason": ui("已有下载在跑——先取消或等它完成",
                                 "a download is already running — cancel it "
                                 "or wait")}
        # fresh task record; .part files in the target resume by themselves
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save({"kind": kind, "size": size, "target": target,
               "state": "running", "files": [], "error": None,
               "error_code": None, "error_detail": None})
        _cancel.clear()
        _thread = threading.Thread(target=_run, args=(kind, target, size),
                                   daemon=True, name="vx-download")
        _thread.start()
        log.info("download: start kind=%s size=%s target=%s",
                 kind, size, target)
    return {"ok": True, **status()}


def cancel() -> dict:
    _cancel.set()
    st = _load()
    if st.get("state") != "running":
        return {"ok": True, "state": st.get("state") or "idle"}
    return {"ok": True, "cancelling": True, **status()}
