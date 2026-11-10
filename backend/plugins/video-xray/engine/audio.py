"""ASR bridge (hermes side, stdlib): PCM -> timestamped cue text.

Engine ladder (generalization pass, 11-09): the user picks
auto / r2t2 / whisper / cloud (or turns transcription off entirely);
"auto" walks whatever this machine actually has installed --

  r2t2     Confucius4-R2T2 short-lived worker (CPU ggml; zero resident VRAM).
           The default runtime root derives from this file's location:
           audio.py lives in <HERMES_HOME>/plugins/video-xray/engine/, so
           three levels up is HERMES_HOME and the sibling asr-r2t2/ is the
           engine root -- works with zero configuration on a machine that
           has the runtime; other machines simply skip this rung (or point
           VA_R2T2_ROOT at it).
  whisper  faster-whisper short-lived worker (engine/asr_worker.py) under a
           user-supplied venv python: video-xray.asr.whisper_python in
           config.yaml or VA_WHISPER_PY. The worker itself prefers CUDA,
           then falls back to CPU int8 by itself.
  cloud    OpenAI-compatible POST {base}/v1/audio/transcriptions. Setup is
           panel-first: type URL + model + key in the panel, press 测试连通
           (reads /v1/models, really transcribes 0.2 s of silence), then
           保存 -- persisted in asr-engines.json, no config.yaml needed.
           Power users keep the yaml/env route (config video-xray.asr.cloud.*
           or VA_ASR_CLOUD_BASE/MODEL/KEY); priority env > panel > config.

First success wins. An explicit single-engine choice never silently
downgrades: it raises with the concrete missing-config reason. Every failed
rung is collected into one error so the pipeline can mark the job
"transcription failed" and still deliver the video-only report.

CPU recipe (master's rule, 11-05): engine threads stay at 4 + low process
priority + Job Object whole-group CPU cap -- default 50%, tunable via
video-xray.asr.cpu_percent or VA_ASR_CPU_PERCENT. A heavy transcription
must never pin every core. Workers exit when done: zero resident VRAM, no
zombies.
"""
import ctypes
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

from . import llm, sampling

ui = llm.ui
# NOTE: do not import from .pipeline at module level -- pipeline imports
# audio, so a top-level back-import hits a partially initialized module
# (cold-start ImportError). _ts is pulled in lazily where used.

# Default engine root derived from this file's location: audio.py lives in
# <HERMES_HOME>/plugins/video-xray/engine/, so three levels up is
# HERMES_HOME and asr-r2t2 next to plugins/ is the engine root. Works on any
# machine with zero configuration; set VA_R2T2_ROOT to point elsewhere.
_HERMES_HOME = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3))
# Default locations. The panel's "Load local engine" button registers
# folders into asr-engines.json (see _load_state below), which beats these
# defaults; VA_R2T2_ROOT/VA_ASR_PY/VA_ASR_WORKER env still win over the
# registry. A fresh user never has to touch config.yaml for paths.
_ASR_ROOT = os.environ.get("VA_R2T2_ROOT",
                           os.path.join(_HERMES_HOME, "asr-r2t2"))
_ASR_PY = os.environ.get("VA_ASR_PY",
                         os.path.join(_ASR_ROOT, "runtime", "python", "python.exe"))
_WORKER = os.environ.get("VA_ASR_WORKER", os.path.join(_ASR_ROOT, "worker.py"))

# whisper python fallback filled from the panel registry; env VA_WHISPER_PY
# and config video-xray.asr.whisper_python still beat it.
_WHISPER_PY_FALLBACK = ""

# Panel registrations persist here (survives restarts; config.yaml optional)
_STATE = os.path.join(_HERMES_HOME, "asr-engines.json")
ENGINE = "auto"  # fallback engine choice when nobody configured one


def _load_state():
    """Registry file shape: {"engines": {"r2t2": <root>, "whisper": <python>},

    "cpu_percent": 50}. Missing or corrupt file yields the empty shape --
    a first run is not an error."""
    try:
        with open(_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("engines"), dict):
            return d
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 -- corrupt json counts as a first run
        pass
    return {"engines": {}}


def _save_state(st):
    try:
        tmp = _STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATE)
    except OSError:  # read-only HERMES_HOME: the session still works
        pass


def _apply_registered(st):
    """Merge the panel registry into the module paths (env vars win)."""
    global _ASR_ROOT, _ASR_PY, _WORKER, _WHISPER_PY_FALLBACK
    root = str(st["engines"].get("r2t2") or "")
    if root and not os.environ.get("VA_R2T2_ROOT"):
        _ASR_ROOT = root
        if not os.environ.get("VA_ASR_PY"):
            py = os.path.join(root, "runtime", "python", "python.exe")
            if os.path.isfile(py):
                _ASR_PY = py
        if not os.environ.get("VA_ASR_WORKER"):
            wk = os.path.join(root, "worker.py")
            if os.path.isfile(wk):
                _WORKER = wk
    _WHISPER_PY_FALLBACK = str(st["engines"].get("whisper") or "")
    cpu = st.get("cpu_percent")
    if isinstance(cpu, int) and 10 <= cpu <= 100 and not os.environ.get("VA_ASR_CPU_PERCENT"):
        os.environ["VA_ASR_CPU_PERCENT"] = str(cpu)


def _reconfigure():
    """Reload the registry and repoint everything reading module globals.

    Runs at import and after every register / CPU save, so the panel
    takes effect without a gateway restart."""
    _apply_registered(_load_state())


_reconfigure()

# ---------------------------------------------------------------------------
# advanced engine parameters (panel 「参数设置」 modal)
# ---------------------------------------------------------------------------
# Stored inside the same asr-engines.json registry under "params" (per
# engine) and "audio" (shared knobs every local engine honors). Saving goes
# through the same live-load probe as engine registration: a broken combo
# (CUDA picked without the native-cuda tree, VRAM too tight for the quant)
# is rejected with the worker's own error words, never persisted.
R2T2_DEVICES = ("cpu", "cuda")
R2T2_QUANTS = ("Q8_0", "Q4_K_M")
WHISPER_DEVICES = ("auto", "cpu", "cuda")
WHISPER_SIZES = ("tiny", "base", "small", "medium", "large-v3")
AUDIO_CHANNELS = (0, 1, 2)   # 0 = default downmix, 1 = left, 2 = right
# Mirror of the worker's confucius.native.SUPPORTED_LANGUAGES (the gateway
# python cannot import the worker's native bindings, so the list ships here
# for the panel dropdown; the worker re-validates and falls back to auto).
R2T2_LANGUAGES = (
    "Arabic", "Cantonese", "Chinese", "Czech", "Danish", "Dutch", "English",
    "Finnish", "French", "German", "Hebrew", "Hindi", "Icelandic", "Indonesian",
    "Italian", "Japanese", "Korean", "Malay", "Norwegian", "Persian", "Polish",
    "Portuguese", "Russian", "Spanish", "Swedish", "Thai", "Turkish", "Vietnamese",
    "Yue", "Greek", "Turkmen", "Urdu", "Serbian")
_WHISPER_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_worker.py")


def _reg_get(section):
    v = _load_state().get(section)
    return dict(v) if isinstance(v, dict) else {}


def engine_params(eng):
    """Saved advanced params for one engine ({} when never saved)."""
    return _reg_get("params").get(eng) or {}


def audio_params():
    """Saved shared track/channel/offset knobs ({} = all defaults)."""
    return _reg_get("audio")


def _save_reg_section(section, values):
    st = _load_state()
    if section == "params":
        cur = st.get("params") if isinstance(st.get("params"), dict) else {}
        cur.update(values)
        st["params"] = cur
    else:
        st["audio"] = values
    _save_state(st)
    return st


def _norm_engine_params(eng, raw):
    """Whitelist-normalize one engine's params. Raises ValueError carrying a
    bilingual reason for anything out of range; unknown keys are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    if eng == "r2t2":
        dev = str(raw.get("device") or "cpu").strip().lower()
        if dev not in R2T2_DEVICES:
            raise ValueError(ui("设备只支持 CPU / CUDA", "device must be cpu or cuda"))
        out["device"] = dev
        quant = str(raw.get("quantization") or "Q8_0").strip().upper()
        if quant not in R2T2_QUANTS:
            raise ValueError(ui("量化只支持 Q8_0 / Q4_K_M",
                                "quantization must be Q8_0 or Q4_K_M"))
        out["quantization"] = quant
        lang = str(raw.get("language") or "").strip()
        if lang and lang not in R2T2_LANGUAGES:
            raise ValueError(ui(f"语言 {lang} 不在 R2T2 支持列表",
                                f"language {lang} is not in the R2T2 list"))
        out["language"] = "" if lang.lower() in ("", "auto") else lang
        out["hotwords"] = str(raw.get("hotwords") or "").strip()[:2000]
    elif eng == "whisper":
        dev = str(raw.get("device") or "auto").strip().lower()
        if dev not in WHISPER_DEVICES:
            raise ValueError(ui("设备只支持 自动 / CPU / CUDA",
                                "device must be auto, cpu or cuda"))
        out["device"] = dev
        size = str(raw.get("model_size") or "small").strip().lower()
        if size not in WHISPER_SIZES:
            raise ValueError(ui("模型尺寸只支持 " + "/".join(WHISPER_SIZES),
                                "model_size must be one of " + "/".join(WHISPER_SIZES)))
        out["model_size"] = size
        lang = str(raw.get("language") or "").strip()
        out["language"] = "" if lang.lower() in ("", "auto") else lang
        out["hotwords"] = str(raw.get("hotwords") or "").strip()[:2000]
    else:
        raise ValueError(ui("该引擎没有参数设置", "this engine has no parameters"))
    return out


def _norm_audio_params(raw):
    """Shared track/channel/offset knobs (all local engines)."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        stream = int(raw.get("stream", 0))
    except (TypeError, ValueError):
        raise ValueError(ui("音轨序号必须是整数", "track index must be an integer"))
    if not 0 <= stream <= 31:
        raise ValueError(ui("音轨序号范围 0-31", "track index must be 0-31"))
    try:
        ch = int(raw.get("channel", 0))
    except (TypeError, ValueError):
        raise ValueError(ui("声道只支持 自动/左/右", "channel must be auto/left/right"))
    try:
        off = float(raw.get("offset", 0.0))
    except (TypeError, ValueError):
        raise ValueError(ui("时间偏移必须是秒数", "offset must be seconds (a number)"))
    if not -600.0 <= off <= 6000.0:
        raise ValueError(ui("时间偏移范围 -600 到 6000 秒",
                            "offset must be between -600 and 6000 seconds"))
    return {"stream": stream, "channel": ch if ch in AUDIO_CHANNELS else 0,
            "offset": off}


def _r2t2_env(extra=None):
    """Worker env for r2t2: saved params overridden by extra (form draft)."""
    p = {**engine_params("r2t2"), **(extra or {})}
    dev = str(p.get("device") or "cpu").strip().lower()
    env = {"VA_R2T2_DEVICE": "cuda" if dev == "cuda" else "cpu",
           "VA_R2T2_QUANT": str(p.get("quantization") or "Q8_0").strip().upper(),
           "VA_R2T2_LANG": str(p.get("language") or "")}
    hw = str(p.get("hotwords") or "").strip()
    if hw:
        env["VA_R2T2_HOTWORDS"] = hw
    return env


def _whisper_env(extra=None):
    """Worker env for whisper: saved params overridden by extra."""
    p = {**engine_params("whisper"), **(extra or {})}
    env = {"VA_ASR_DEVICE": str(p.get("device") or "auto").strip().lower(),
           "VA_ASR_MODEL_SIZE": str(p.get("model_size") or "small").strip().lower(),
           "VA_ASR_LANG": str(p.get("language") or "")}
    hw = str(p.get("hotwords") or "").strip()
    if hw:
        env["VA_ASR_PROMPT"] = hw
    return env


def probe_audio_tracks(video):
    """(count, reason): number of audio streams in the file (ffprobe), so
    the panel can offer exactly the selectable track indexes."""
    try:
        p = subprocess.run(
            [sampling._ffprobe(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, timeout=30)
    except FileNotFoundError:
        return 0, ui("找不到 ffprobe", "ffprobe not found")
    except Exception as e:  # noqa: BLE001
        return 0, ui(f"探测音轨失败：{e}", f"track probe failed: {e}")
    if p.returncode != 0:
        tail = " ".join((p.stderr or b"").decode("utf-8", "replace").split())[-200:]
        return 0, ui(f"探测音轨失败：{tail or 'ffprobe 报错'}",
                     f"track probe failed: {tail or 'ffprobe errored'}")
    n = len([ln for ln in (p.stdout or b"").decode("utf-8", "replace").splitlines()
             if ln.strip()])
    return n, ""


def _shift_cues(cues, offset):
    """Subtitle-drift correction applied to every local engine's cues
    (cloud cues shift inside _cloud_transcribe; the no-timestamp plain-text
    fallback has nothing to shift)."""
    if not offset or not cues:
        return cues
    out = []
    for c in cues:
        try:
            s = max(0.0, float(c.get("s", 0)) + offset)
            e = max(0.0, float(c.get("e", 0)) + offset)
        except (TypeError, ValueError):
            out.append(c)
            continue
        out.append({**c, "s": round(s, 2), "e": round(e, 2)})
    return out


def _offset():
    try:
        return float(audio_params().get("offset") or 0.0)
    except (TypeError, ValueError):
        return 0.0

_BELOW_NORMAL = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS


class _JobCpuRate(ctypes.Structure):
    """JOBOBJECT_CPU_RATE_CONTROL_INFORMATION, union flattened per winnt.h:
    ControlFlags@0, MinRate@4, MaxRate@8, Weight@12 (DWORDs, size 16). The
    old layout stuffed a phantom CpuRate u32 into the union slot and put
    MaxRate at offset 10 -- SetInformationJobObject rejected every set."""
    _fields_ = [("ControlFlags", ctypes.c_uint32),
                ("MinRate", ctypes.c_uint32),
                ("MaxRate", ctypes.c_uint32),
                ("Weight", ctypes.c_uint32)]


class _BasicLimit(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION; only LimitFlags=KILL_ON_JOB_CLOSE
    is used, to keep orphans from surviving."""
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


def _spawn_cpu_capped(cmd, env, rate_percent=50):
    """Popen + (Windows) Job Object whole-group CPU cap + low priority; the
    cap throttles, it never kills.

    Returns (proc, job_handle); the caller CloseHandle's job_handle after
    the process ends. KILL_ON_JOB_CLOSE is the safety net: if the gateway
    dies abruptly the worker dies with the job handle, leaving nothing
    behind.
    """
    flags = _BELOW_NORMAL if os.name == "nt" else 0
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, creationflags=flags)
    job = None
    if os.name == "nt":
        k = ctypes.windll.kernel32
        h = k.CreateJobObjectW(None, None)
        if h:
            # Declared types: without them ctypes passes the 64-bit job
            # handle as a 32-bit int and AssignProcessToJobObject would
            # silently get a truncated handle.
            k.CreateJobObjectW.restype = ctypes.c_void_p
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k.SetInformationJobObject.restype = ctypes.c_int
            k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                  ctypes.c_void_p, ctypes.c_uint32]
            k.AssignProcessToJobObject.restype = ctypes.c_int
            k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            # 1) KILL_ON_JOB_CLOSE: the non-negotiable orphan guard.
            lim = _BasicLimit()
            lim.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            basic_ok = bool(k.SetInformationJobObject(
                h, 1, ctypes.byref(lim), ctypes.sizeof(lim)))
            # 2) CPU-rate cap: best-effort only. Some sessions (notably RDP
            # logons) reject it outright -- thread caps + low priority still
            # apply there. It must NEVER veto the orphan guard: the old
            # `if ok and Assign` coupled them, so a rejected rate silently
            # un-mounted KILL_ON_JOB_CLOSE too.
            info = _JobCpuRate()
            info.ControlFlags = 0x1 | 0x2  # ENABLE | MIN_MAX_RATE (winnt.h)
            info.MinRate = 0               # no floor
            info.MaxRate = int(rate_percent) * 100  # per-mille
            k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))
            if basic_ok and k.AssignProcessToJobObject(h, p._handle):
                job = h
            if not job:
                k.CloseHandle(h)
    return p, job


def _pcm_to_wav(pcm16k):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16k)
    return buf.getvalue()


def _run_worker(cmd, wav_bytes, timeout, cpu_pct, extra_env=None, cancel=None):
    """Spawn one short-lived worker under the CPU cap, feed it WAV, parse
    the cues JSON printed on its last stdout lines. Returns cue list or
    None (silent audio). extra_env carries the panel's advanced engine
    parameters (device/quant/language/hotwords) to the worker. cancel is
    the job's should_stop callable: a watcher thread polls it and force-
    kills the worker the moment the user cancels, so no analysis process
    lingers after cancel/error."""
    # subprocess isolation: the gateway harness leaks its PYTHONPATH into
    # children and hijacks package resolution -- strip it
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(extra_env or {})
    # Polite-cancel sentinel: unique path per run, NOT created yet -- the
    # watcher touches it when the user cancels and the worker's poll thread
    # sees it and exits itself (see asr-r2t2/worker.py), which lets the CUDA
    # driver reclaim VRAM synchronously instead of leaving a deferred block.
    cancel_file = None
    if cancel is not None:
        cancel_file = os.path.join(
            tempfile.gettempdir(),
            "xray-cancel-" + uuid.uuid4().hex + ".tmp")
        env["VA_CANCEL_FILE"] = cancel_file
    p, job = _spawn_cpu_capped(cmd, env=env, rate_percent=cpu_pct)
    # Cancel plumbing (owner 2026-11-09): communicate() below blocks for the
    # whole transcription -- minutes on long audio -- so without a watcher a
    # user cancel would leave the worker chewing CPU/GPU long after the job
    # was cancelled ("force the analysis processes out, else the brain
    # sticks"). A daemon watcher polls the job's cancel flag and kills the
    # process the moment it trips; KILL_ON_JOB_CLOSE still releases the job
    # object in the finally below.
    if cancel is not None:
        def _watch():
            while p.poll() is None:
                try:
                    if cancel():
                        # Polite first (owner 2026-11-10): touch the sentinel
                        # and give the worker time to exit on its own -- a
                        # self-exit lets the CUDA driver reclaim the context
                        # synchronously, so VRAM drops right away and users
                        # don't see a "leak" in task manager. Hard-kill only
                        # if it ignores the signal.
                        if cancel_file:
                            try:
                                open(cancel_file, "wb").close()
                            except OSError:
                                pass
                            for _ in range(8):      # ~3.6 s grace
                                time.sleep(0.45)
                                if p.poll() is not None:
                                    break
                        if p.poll() is None:
                            try:
                                p.kill()
                            except OSError:
                                pass
                        return
                except Exception:
                    return
                time.sleep(0.4)
        threading.Thread(target=_watch, daemon=True).start()
    try:
        try:
            out, err = p.communicate(input=wav_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            raise RuntimeError(ui(f"ASR 工人超时 ({timeout}s)",
                                  f"ASR worker timed out ({timeout}s)"))
    finally:
        if job:
            ctypes.windll.kernel32.CloseHandle(job)
        if cancel_file:
            try:
                os.remove(cancel_file)
            except OSError:
                pass
    if p.returncode != 0 and cancel is not None:
        # the watcher killed it on user cancel -- that is not an engine
        # failure: speak "cancelled", never "failed"
        try:
            _cx = bool(cancel())
        except Exception:
            _cx = False
        if _cx:
            raise RuntimeError(ui("语音转录已取消",
                                  "speech transcription cancelled"))
    if p.returncode != 0:
        raise RuntimeError(ui(f"ASR 工人失败: {err.decode('utf-8', 'ignore')[-1500:]}",
                              f"ASR worker failed: {err.decode('utf-8', 'ignore')[-1500:]}"))
    tail = out.decode("utf-8", "ignore").strip().splitlines()
    if not tail:
        return None
    # progress/warning noise sometimes mixes into the last lines: scan back
    # for the first line that parses as a list. A bare json.loads(tail[-1])
    # turns one stray print into a gibberish error; include stderr on failure.
    cues = None
    for ln in reversed(tail[-25:]):
        if ln.startswith("["):
            try:
                parsed = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                cues = parsed
                break
    if cues is None:
        raise RuntimeError(ui(
            f"ASR 输出不是 cues JSON：{tail[-1][:180]} "
            f"err={err.decode('utf-8', 'ignore')[-300:]}",
            f"ASR output is not cues JSON: {tail[-1][:180]} "
            f"err={err.decode('utf-8', 'ignore')[-300:]}"))
    return cues


def _cues_text(cues):
    """Cues -> the plain transcript block the report layer consumes. Also
    the single choke point where the panel's subtitle-drift offset lands,
    so every engine's timings shift consistently."""
    if not cues:
        return None
    cues = _shift_cues(cues, _offset())
    from .pipeline import _ts   # lazy: keeps the pipeline<->audio import cycle cold-start safe
    lines = [f"{_ts(c['s'])} {c['text']}" for c in cues]
    return "\n".join(lines) if lines else None


def _whisper_python():
    return (os.environ.get("VA_WHISPER_PY", "").strip()
            or llm.config_get("video-xray", "asr", "whisper_python") or ""
            or _WHISPER_PY_FALLBACK)


def _norm_base(u):
    """Accept sloppy novice input: missing scheme, trailing slash, an
    .../v1 suffix typed by habit. Returns a bare host base like
    https://api.example.com."""
    u = str(u or "").strip().strip('"').rstrip("/")
    if u and "://" not in u:
        u = "https://" + u
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def _registry_cloud_cfg():
    """Cloud ASR saved through the panel (asr-engines.json "cloud")."""
    c = _load_state().get("cloud")
    if not isinstance(c, dict):
        return None
    base = str(c.get("base_url") or "").strip().rstrip("/")
    model = str(c.get("model") or "").strip()
    if not base or not model:
        return None
    return {"base": base, "model": model,
            "key": str(c.get("api_key") or "").strip()}


def _cloud_cfg():
    """OpenAI-compatible cloud ASR target, or None when not configured.
    Priority: env > panel registry (测试连通 + 保存) > config.yaml, so a
    panel save always wins over a stale yaml value -- and power users can
    still pin with env vars without touching either file."""
    reg = _registry_cloud_cfg()
    base = (os.environ.get("VA_ASR_CLOUD_BASE", "").strip()
            or (reg and reg["base"])
            or llm.config_get("video-xray", "asr", "cloud", "base_url") or "")
    model = (os.environ.get("VA_ASR_CLOUD_MODEL", "").strip()
             or (reg and reg["model"])
             or llm.config_get("video-xray", "asr", "cloud", "model") or "")
    if not base or not model:
        return None
    key = (os.environ.get("VA_ASR_CLOUD_KEY", "").strip()
           or os.environ.get("OPENAI_API_KEY", "").strip()
           or (reg and reg["key"])
           or llm.config_get("video-xray", "asr", "cloud", "api_key") or "")
    return {"base": base.rstrip("/"), "model": model, "key": key}


def _http_json(url, key="", data=None, content_type=None, timeout=15):
    """Small JSON-over-HTTP helper for the cloud probes (stdlib only)."""
    h = {"Accept": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    if content_type:
        h["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def cloud_models(base, key, timeout=15):
    """GET {base}/v1/models -- powers the cloud model dropdown.
    Returns (ids, reason); reason == '' on success. A server without the
    endpoint is not fatal: the caller can still transcribe a known model."""
    try:
        res = _http_json(base + "/v1/models", key, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return [], ui("密钥被模型接口拒绝（HTTP %d）：请检查 API 密钥" % e.code,
                          "the models endpoint rejected the key (HTTP %d): check the API key" % e.code)
        return [], ui("读模型列表失败（HTTP %d），不影响直接转写" % e.code,
                      "model list failed (HTTP %d); direct transcription may still work" % e.code)
    except Exception as e:
        return [], ui("地址连不通：%s（%s: %s）——检查 URL、网络或代理" % (
            base, type(e).__name__, str(e)[:150]),
            "cannot reach %s (%s: %s) — check the URL, network or proxy" % (
            base, type(e).__name__, str(e)[:150]))
    items = res.get("data") if isinstance(res, dict) else None
    ids = ([str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
           if isinstance(items, list) else [])
    return ids, ""


def settings():
    """Merged ASR preferences: the config.yaml `video-xray.asr` section with
    env overrides (VA_ASR_ENGINE / VA_ASR_DEVICE / VA_ASR_CPU_PERCENT)."""
    out = {"engine": "", "device": "auto", "cpu_percent": 50}
    for k in ("engine", "device"):
        v = llm.config_get("video-xray", "asr", k)
        if v:
            # str() first: YAML happily hands us ints (device: 0, cache: 1)
            # and a bare .strip() on an int would crash every job's ASR read.
            out[k] = str(v).strip().lower()
    for k, env_key in (("engine", "VA_ASR_ENGINE"), ("device", "VA_ASR_DEVICE")):
        v = os.environ.get(env_key, "").strip().lower()
        if v:
            out[k] = v
    v = str(os.environ.get("VA_ASR_CPU_PERCENT", "")
            or llm.config_get("video-xray", "asr", "cpu_percent") or "").strip()
    if v:
        try:
            out["cpu_percent"] = max(10, min(100, int(float(v))))
        except ValueError:
            pass
    # The panel's "save" outranks env/yaml (layering: Panel > YAML). It used
    # to land in a state file no reader consulted, plus an env var named
    # VA_ASR_CPU that settings() never read -- a dead-end save.
    try:
        st_cpu = _load_state().get("cpu_percent")
        if isinstance(st_cpu, (int, float)) and 10 <= int(st_cpu) <= 100:
            out["cpu_percent"] = int(st_cpu)
    except Exception:
        pass
    return out


def engines_available():
    """What this machine can transcribe with right now (panel hint + health)."""
    py = _whisper_python()
    return {"r2t2": os.path.isfile(_ASR_PY) and os.path.isfile(_WORKER),
            "whisper": bool(py) and os.path.isfile(py) and os.path.isfile(_WHISPER_WORKER),
            "cloud": bool(_cloud_cfg())}


def _missing_reason(eng):
    if eng == "r2t2":
        return ui(f"R2T2 运行时不在 {_ASR_ROOT}：点面板上的「本地引擎装填…」按钮选它的"
                  "目录即可（也可以把目录放到 HERMES_HOME 下，或用环境变量 "
                  "VA_R2T2_ROOT 指路）",
                  f"R2T2 runtime is not at {_ASR_ROOT}: click \"Load local engine...\" "
                  "in the panel and pick its folder (or place it under HERMES_HOME, "
                  "or point VA_R2T2_ROOT at it)")
    if eng == "whisper":
        return ui("本地 Whisper 未装载：点面板上的「本地引擎装填…」按钮选装了 "
                  "faster-whisper 的 venv 根目录（也可在 config.yaml 填 "
                  "video-xray.asr.whisper_python 或用环境变量 VA_WHISPER_PY）",
                  "local Whisper is not loaded: click \"Load local engine...\" in the "
                  "panel and pick the root of a venv that has faster-whisper installed "
                  "(or set video-xray.asr.whisper_python / VA_WHISPER_PY)")
    return ui("云端 ASR 未配置：在面板转录引擎区的「云端 ASR」填地址+密钥，点「测试连通」"
              "通过后按「保存」即可（老手也可在 config.yaml 填 video-xray.asr.cloud."
              "base_url 和 .model，密钥放 .api_key 或环境变量 OPENAI_API_KEY）",
              "cloud ASR is not configured: fill the URL + key in the panel's "
              "Cloud ASR row, press \"Test connection\" then \"Save\" (power users "
              "can also set video-xray.asr.cloud.base_url/.model in config.yaml, "
              "key via .api_key or OPENAI_API_KEY)")


def _r2t2_transcribe(pcm_bytes, timeout, cpu_pct, cancel=None):
    return _cues_text(_run_worker([_ASR_PY, _WORKER], _pcm_to_wav(pcm_bytes),
                                  timeout, cpu_pct, _r2t2_env(),
                                  cancel=cancel))


def _whisper_transcribe(pcm_bytes, timeout, cpu_pct, cancel=None):
    return _cues_text(_run_worker([_whisper_python(), _WHISPER_WORKER],
                                  _pcm_to_wav(pcm_bytes), timeout, cpu_pct,
                                  _whisper_env(), cancel=cancel))


def _cloud_transcribe(pcm_bytes, timeout, cfg=None):
    cfg = cfg or _cloud_cfg()
    if not cfg:
        raise RuntimeError(_missing_reason("cloud"))
    base = cfg["base"]
    url = (base if base.endswith("/v1") else base + "/v1") + "/audio/transcriptions"
    boundary = "videoXrayBoundary"
    body = bytearray()
    for name, val in (("model", cfg["model"]),
                      ("response_format", "verbose_json"),
                      ("timestamp_granularities[]", "segment")):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                 f"{val}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode("utf-8")
    body += _pcm_to_wav(pcm_bytes) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if cfg["key"]:
        headers["Authorization"] = "Bearer " + cfg["key"]
    req = urllib.request.Request(url, data=bytes(body), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 -- one clean message for network/HTTP/JSON errors
        raise RuntimeError(ui(f"云端 ASR 调用失败: {e}", f"cloud ASR call failed: {e}")) from e
    segs = res.get("segments") if isinstance(res, dict) else None
    if isinstance(segs, list) and segs:
        cues = [{"s": round(float(s.get("start") or 0), 2),
                 "e": round(float(s.get("end") or 0), 2),
                 "text": str(s.get("text") or "").strip()}
                for s in segs if isinstance(s, dict) and str(s.get("text") or "").strip()]
        return _cues_text(cues)
    text = str((res or {}).get("text") or "").strip()
    if not text:
        return None
    # servers that ignore verbose_json answer with one plain text blob
    from .pipeline import _ts  # lazy cycle guard
    return f"{_ts(0)} {text}"


def transcribe_cues(pcm_bytes, engine=None, timeout=900, cancel=None):
    """Transcribe window PCM. Returns (text, engine_used) or (None, used/None).

    `engine` (tool/panel param: auto/r2t2/whisper/cloud) wins over the
    config/env choice. "auto": walk the ladder over whatever
    engines_available() reports -- r2t2 -> whisper -> cloud; device=gpu
    reorders to whisper-first (its worker prefers CUDA). An explicit engine
    that is not installed raises with its missing-config reason instead of
    silently downgrading. When auto finds nothing usable, or every rung
    fails, RuntimeError lists every attempt -- the pipeline catches it,
    notes the report and the job continues video-only.
    """
    if not pcm_bytes:
        return None, None
    s = settings()
    eng = (engine or s["engine"] or "auto").strip().lower()
    if eng in ("", "auto"):
        eng = "auto"
    device = s["device"]
    cpu = s["cpu_percent"]
    avail = engines_available()
    impl = {"r2t2": lambda: _r2t2_transcribe(pcm_bytes, timeout, cpu, cancel=cancel),
            "whisper": lambda: _whisper_transcribe(pcm_bytes, timeout, cpu, cancel=cancel),
            "cloud": lambda: _cloud_transcribe(pcm_bytes, min(timeout, 180))}
    if eng != "auto":
        if eng not in impl:
            raise RuntimeError(ui(f"未知的转录引擎 {eng}", f"unknown ASR engine {eng}"))
        if not avail[eng]:
            raise RuntimeError(ui(f"转录引擎 {eng} 不可用：", f"ASR engine {eng} unavailable: ")
                               + _missing_reason(eng))
        return impl[eng](), eng
    ladder = ("whisper", "r2t2", "cloud") if device == "gpu" else ("r2t2", "whisper", "cloud")
    errors = []
    for name in ladder:
        if not avail[name]:
            continue
        try:
            return impl[name](), name
        except Exception as e:  # noqa: BLE001 -- collect and try the next rung
            if cancel is not None:
                # user cancelled mid-rung: never "helpfully" start the next
                # engine over a cancelled job -- bail out immediately
                try:
                    _cx = bool(cancel())
                except Exception:
                    _cx = False
                if _cx:
                    raise RuntimeError(ui("语音转录已取消",
                                          "speech transcription cancelled"))
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
    if errors:
        raise RuntimeError(ui("所有可用转录引擎都失败了：\n",
                              "every available ASR engine failed:\n") + "\n".join(errors))
    raise RuntimeError(ui(
        "这台机器没有任何可用的转录引擎：R2T2 运行时未安装、本地 Whisper 未配 venv、"
        "云端 ASR 未配置。装好任意一个都会自动启用；或把转录引擎设为\"关闭\"只分析画面",
        "no ASR engine is available on this machine: the R2T2 runtime is not installed, no "
        "local Whisper venv is configured, and cloud ASR is not set up. Install or configure "
        "any one of them and transcription enables itself, or choose \"off\" for "
        "video-only analysis"))


# --- engine registration (panel-first; config.yaml stays the power-user path) ---

ASR_ENGINES = ("r2t2", "whisper", "cloud")


def _test_whisper(py):
    """One-shot import probe so a wrong venv fails here, not mid-job.

    Returns (ok, reason). The 60 s ceiling gives a cold interpreter its
    room, but a hung python never holds the panel hostage."""
    if not os.path.isfile(_WHISPER_WORKER):
        return False, ui(f"插件缺少随附工人脚本：{_WHISPER_WORKER}",
                         f"the bundled worker script is missing: {_WHISPER_WORKER}")
    try:
        p = subprocess.run(
            [py, "-c", "import faster_whisper"], capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": "", "PYTHONHOME": ""})
    except Exception as e:  # noqa: BLE001 -- timeout/exec errors: one clean reason
        return False, ui(f"用 {py} 试跑失败：{e}", f"test-run with {py} failed: {e}")
    if p.returncode == 0:
        return True, ""
    err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return False, ui(f"{py} 没装 faster-whisper（试跑报错：{err[-1] if err else '未知'}）",
                     f"faster-whisper is not installed for {py} "
                     f"(test-run said: {err[-1] if err else 'unknown error'})")


_SILENCE = b"\x00" * 6400  # 0.2 s of 16 kHz mono s16le silence


def _verify_load(cmd, timeout, extra_env=None):
    """The promise the panel 保存 button makes: really boot the engine,
    load the model, transcribe 0.2 s of silence. A folder with the right
    shape that cannot actually load (broken venv, missing model files,
    dead DLL) never registers and never lights its circle green.

    Same short-lived worker as real transcription: it exits either way,
    so whatever VRAM/CPU the probe touched comes straight back (Windows
    reclaims handles on process exit; Job Object KILL_ON_JOB_CLOSE is the
    orphan net). extra_env carries the panel's advanced engine parameters
    so the probe validates exactly the settings that would be saved.
    Returns (ok, reason)."""
    try:
        _run_worker(cmd, _pcm_to_wav(_SILENCE), timeout,
                    settings()["cpu_percent"], extra_env)
    except Exception as e:  # noqa: BLE001 -- one clean panel reason
        tail = " ".join(str(e).split())[-300:]
        return False, ui("引擎能选但真机加载验证失败：" + (tail or "无输出"),
                         "the folder looks right but the engine failed to load: "
                         + (tail or "no output"))
    return True, ""


def _cloud_verify(base, key, model):
    """The shared cloud promise: GET /v1/models (powers the dropdown) and a
    REAL transcription of 0.2 s silence through /v1/audio/transcriptions --
    URL, key and audio endpoint proven together before anything is saved.
    Silence usually answers empty text: an HTTP 200 IS the pass.

    Returns (ok, models, model_used, reason). reason may carry a soft
    warning (model list unreadable) even when ok is True."""
    base = _norm_base(base)
    if not base:
        return False, [], "", ui("云端地址没填", "no cloud URL was given")
    models, merr = cloud_models(base, key)
    model = str(model or "").strip()
    if not model:
        if not models:
            return False, [], "", ui(
                (merr + "；也没手填模型名" if merr else
                 "读不到模型列表（/v1/models），请手填模型名"),
                (merr + "; no model name typed either" if merr else
                 "no model list from /v1/models -- type the model name manually"))
        model = models[0]
    try:
        _cloud_transcribe(_SILENCE, 30, {"base": base, "model": model, "key": key})
    except Exception as e:  # noqa: BLE001 -- one clean panel reason
        msg = str(e)[:250]
        if any(t in msg for t in ("401", "403", "Unauthorized", "invalid_api_key")):
            why = ui("地址能连上，但转写接口拒绝了这把密钥（401/403）：" + msg,
                     "the server answered but rejected the key at the "
                     "transcription endpoint (401/403): " + msg)
        else:
            why = ui("转写接口实测失败：" + msg,
                     "live transcription probe failed: " + msg)
        return False, models, model, why
    return True, models, model, merr


def _resolve_key(api_key):
    """Panel key field: typed value wins; empty falls back to the saved key
    (the field shows only its tail, so empty means 'unchanged'); still
    empty = public server, no Authorization header."""
    key = str(api_key if api_key is not None else "").strip()
    if not key:
        key = (_registry_cloud_cfg() or {}).get("key", "")
    return key


def test_cloud(base_url, api_key=None, model=None):
    """Panel 「测试连通」: never raises; always reports how far it got.

    {ok, models, model_used, reason} -- the model list is returned even on
    a partial pass (address alive, audio endpoint dead) so the dropdown
    still fills and the novice sees exactly what failed. Nothing persists."""
    ok, models, used, why = _cloud_verify(base_url, _resolve_key(api_key), model)
    return {"ok": ok, "models": models[:80], "model_used": used, "reason": why}


def save_cloud(base_url, api_key=None, model=None):
    """Panel 「保存」 for cloud ASR: same live test first, registry written
    only on a pass -- saving a dead endpoint would light the circle green
    and then fail every job. Returns {ok, ...} or {ok: False, reason}."""
    key = _resolve_key(api_key)
    ok, models, used, why = _cloud_verify(base_url, key, model)
    if not ok:
        return {"ok": False, "reason": why}
    st = _load_state()
    st["cloud"] = {"base_url": _norm_base(base_url), "model": used, "api_key": key}
    _save_state(st)
    avail = engines_available()
    return {"ok": True, "base_url": st["cloud"]["base_url"], "model": used,
            "key_tail": (key[-4:] if key else ""),
            "loaded": sorted(e for e in ASR_ENGINES if avail.get(e))}


def register_asr_engine(path):
    """Register a transcription engine folder picked through the panel.

    Recognized shapes:
      R2T2    folder with worker.py + runtime/python/python.exe -> r2t2
      venv    folder with Scripts/python.exe or bin/python(3) that can
              import faster_whisper -> whisper (the python path is what
              gets registered)

    Shape check alone never registers anything: BOTH engines must pass
    _verify_load -- a real worker run that loads the model and transcribes
    0.2 s of silence -- before asr-engines.json is written. A pick that
    only looks right fails here with the engine's own words, nothing is
    saved and the probe leaves no VRAM/CPU behind.

    Priority: env > config.yaml whisper_python > registry (config.yaml
    pins an explicit python, an explicit choice must win). Returns
    {ok, engine|reason, path, loaded:[...]} -- never raises on bad input."""
    path = str(path or "").strip().strip('"')
    if not path:
        return {"ok": False, "reason": ui("没选目录", "no folder was chosen")}
    if not os.path.isdir(path):
        return {"ok": False, "reason": ui(f"目录不存在：{path}",
                                          f"folder does not exist: {path}")}

    if os.path.isfile(os.path.join(path, "worker.py")) and os.path.isfile(
            os.path.join(path, "runtime", "python", "python.exe")):
        eng, registered = "r2t2", path
        # shape alone never lights the circle: boot THIS runtime and really
        # load the model on a 0.2 s silence clip before anything is saved.
        # --probe forces the Q8 engine + VAD load: a plain silence clip
        # would hit the worker's VAD short-circuit and exit without ever
        # touching the model, turning the check into a fake green.
        ok, why = _verify_load(
            [os.path.join(path, "runtime", "python", "python.exe"),
             os.path.join(path, "worker.py"), "--probe"], 180, _r2t2_env())
        if not ok:
            return {"ok": False, "reason": why}
    else:
        cand = [os.path.join(path, "Scripts", "python.exe"),
                os.path.join(path, "bin", "python"),
                os.path.join(path, "bin", "python3"),
                os.path.join(path, "python.exe")]
        found = next((c for c in cand if os.path.isfile(c)), "")
        if not found:
            return {"ok": False, "reason": ui(
                "认不出来：这个目录既没有 worker.py + runtime\\python\\python.exe"
                "（R2T2 运行时根目录），也没有 Scripts\\python.exe 或 bin/python"
                "（装了 faster-whisper 的 venv 根目录）",
                "unrecognized folder: it has neither worker.py + "
                "runtime/python/python.exe (an R2T2 runtime root) nor "
                "Scripts/python.exe or bin/python (the root of a venv with "
                "faster-whisper)")}
        eng = "whisper"
        # config.yaml whisper_python pins the venv python: honor it, but
        # only when it exists; the picked venv must still prove the import.
        cfg_py = str(llm.config_get("video-xray", "asr", "whisper_python") or "").strip()
        registered = cfg_py if cfg_py and os.path.isfile(cfg_py) else found
        ok, why = _test_whisper(registered)
        if not ok:
            return {"ok": False, "reason": why}
        # the import probe says the package is there; the save button's real
        # promise is the model loading and transcribing, so prove that too.
        # The first run may download weights (~460 MB), hence the generous
        # timeout; a timeout says so plainly instead of hanging the panel.
        ok, why = _verify_load([registered, _WHISPER_WORKER], 600, _whisper_env())
        if not ok:
            return {"ok": False, "reason": why}

    st = _load_state()
    st["engines"][eng] = registered
    _save_state(st)
    _reconfigure()
    avail = engines_available()
    if not avail.get(eng):
        return {"ok": False, "reason": ui(
            f"登记了 {eng} 但仍探测不可用：{registered}",
            f"registered {eng} but it still probes unavailable: {registered}")}
    return {"ok": True, "engine": eng, "path": registered,
            "loaded": sorted(e for e in ASR_ENGINES if avail.get(e))}


def set_cpu_percent(pct):
    """Panel CPU cap: persist to the registry and apply at once."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return {"ok": False, "reason": ui("cpu_percent 需要是 10-100 的整数",
                                          "cpu_percent must be an integer 10-100")}
    if not 10 <= pct <= 100:
        return {"ok": False, "reason": ui("cpu_percent 只能在 10 到 100 之间",
                                          "cpu_percent must be between 10 and 100")}
    st = _load_state()
    st["cpu_percent"] = pct
    _save_state(st)
    os.environ["VA_ASR_CPU_PERCENT"] = str(pct)  # the one name settings() reads
    return {"ok": True, "cpu_percent": pct}


def asr_settings_view():
    """The settings surface the panel reads (part of GET /health)."""
    s = settings()
    st = _load_state()
    reg = _registry_cloud_cfg()
    cfg = reg or _cloud_cfg()  # what would actually run (env > panel > yaml)
    key = (cfg or {}).get("key") or ""
    return {"engine": s["engine"], "device": s["device"],
            "cpu_percent": s["cpu_percent"],
            "registered": {k: v for k, v in st["engines"].items() if v},
            "config_whisper_python": str(
                llm.config_get("video-xray", "asr", "whisper_python") or "").strip(),
            # cloud row seeding: the key itself never travels to the panel,
            # only its last 4 chars so the user knows one is on file
            "cloud": {"base_url": (cfg or {}).get("base", ""),
                      "model": (cfg or {}).get("model", ""),
                      "key_tail": key[-4:] if key else "",
                      "saved_via_panel": bool(reg)},
            # advanced per-engine knobs + the shared track/channel/offset
            "params": {"r2t2": engine_params("r2t2"),
                       "whisper": engine_params("whisper")},
            "audio": audio_params(),
            "options": {
                "r2t2_devices": list(R2T2_DEVICES),
                "r2t2_quants": list(R2T2_QUANTS),
                "r2t2_languages": list(R2T2_LANGUAGES),
                "whisper_devices": list(WHISPER_DEVICES),
                "whisper_sizes": list(WHISPER_SIZES),
            }}


def _engine_probe(eng, env):
    """Boot the registered engine for `eng` with these exact env knobs
    and really load it (--probe forces the full model + VAD/DLL load).
    Returns (ok, reason); the worker exits either way, so the probe costs
    one engine boot and leaves no VRAM/CPU behind."""
    if eng == "r2t2":
        if not (_ASR_ROOT and _WORKER and _ASR_PY):
            return False, ui(
                "R2T2 运行时还没登记，请先在「本地引擎」里选好目录",
                "the R2T2 runtime is not registered yet; pick its folder "
                "in the engine picker first")
        return _verify_load([_ASR_PY, _WORKER, "--probe"], 180, env)
    py = _whisper_python()
    if not py:
        return False, ui("Whisper venv 还没登记，请先在「本地引擎」里选好目录",
                         "the Whisper venv is not registered yet; pick its "
                         "folder in the engine picker first")
    # loading medium/large weights can take minutes (first run may even
    # download them), so the probe gets the same generous timeout
    return _verify_load([py, _WHISPER_WORKER, "--probe"], 600, env)


def test_engine_params(engine, raw):
    """Params-modal live test, nothing persisted: normalize -> really
    load with these knobs -> report. {ok, reason, params} — never raises
    on bad input."""
    eng = str(engine or "").strip().lower()
    try:
        norm = _norm_engine_params(eng, raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    env = _r2t2_env(norm) if eng == "r2t2" else _whisper_env(norm)
    ok, why = _engine_probe(eng, env)
    return {"ok": ok, "reason": why, "params": norm}


def save_engine_params(engine, raw):
    """Params-modal 保存: the same live test must pass before the knobs
    persist — a device/quant that cannot boot (no CUDA on this box, the
    Q4 weights not downloaded yet) is rejected with the engine's own
    words instead of lighting a trap for the next job."""
    eng = str(engine or "").strip().lower()
    try:
        norm = _norm_engine_params(eng, raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    env = _r2t2_env(norm) if eng == "r2t2" else _whisper_env(norm)
    ok, why = _engine_probe(eng, env)
    if not ok:
        return {"ok": False, "reason": why}
    _save_reg_section("params", {eng: norm})
    return {"ok": True, "engine": eng, "params": norm}


def save_audio_params(raw):
    """Shared 音轨/声道/字幕偏移: pure data-level knobs validated by the
    whitelist — nothing to boot, so normalization IS the verification."""
    try:
        norm = _norm_audio_params(raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    _save_reg_section("audio", norm)
    return {"ok": True, "audio": norm}


def probe_tracks(video_path):
    """Panel helper for the 音轨 dropdown: how many audio streams THIS
    video has (0 = silent). Never raises on bad input."""
    n, err = probe_audio_tracks(video_path)
    if n < 0:
        return {"ok": False, "reason": err}
    return {"ok": True, "tracks": n}
