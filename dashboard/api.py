"""HTTP bridge for the video-xray desktop panel.

The source of truth is always the file ledger under
<HERMES_HOME>\\xray-jobs\\ (engine/jobs.py); this is only a thin wrapper:
the panel polls GET /jobs and GET /jobs/{id} every ~2 s; control goes
through POST (cancel rides on the cancel file, effective across processes,
matching the engine contract); cleanup is POST too (tick-and-delete
/jobs/delete and one-click /jobs/clear -- running jobs are refused/skipped;
DELETE is not used because the current desktop shell's plugin rest only
allows GET/POST).

Mounting: see hermes_cli/web_server_dashboard.py::_mount_one_plugin_api --
the gateway importlib-loads this file, takes the module-level `router`
(FastAPI APIRouter), mounts it under /api/plugins/video-xray/ with the
secret dependency. Auth comes automatically from the frontend context's
fetchAPI/fetchJSON token; a bare curl gets 401.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
import os

# One level up from dashboard/ is the plugin root; engine is a package under it
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from engine import audio, h3prompt, jobs, llm  # noqa: E402

ui = llm.ui

router = APIRouter()

# The list page carries summary fields only: full summary/notes/transcript
# can be tens of KB -- keep them out of the polling.
# The panel's preset dropdown follows the engine SCHEMA_ANALYZE English enum
# (Chinese display names live only in the panel).
_PRESETS = ("default", "quick8", "single32", "single64", "segment24fps", "segment30fps")

# The projection takes the union: short table (done/created_at/resume_of/
# transcript_used/...) plus the rich fields the panel renders (name/
# segments_done/progress/updated_at/params/video/path_exists/...). Missing a
# key blinds one part of the panel; an extra key is harmless (_brief just
# copies whatever the ledger has). path feeds frontend dedup.
_BRIEF_KEYS = ("id", "name", "path", "status", "done", "segments_done", "total",
               "created_at", "started_at", "finished_at", "updated_at",
               "resume_of", "transcript_used", "summary_truncated", "cancel_requested",
               "report", "report_chars", "segments", "progress", "error", "elapsed_s",
               "params", "video", "start", "path_exists", "stop_reason",
               "subtitles", "sub_text", "transcribe", "strategy")
# Note: request bodies are deliberately plain dicts with manual validation,
# not pydantic models. The gateway importlib-loads this file (not necessarily
# into sys.modules); pydantic resolves class annotations via
# sys.modules[__module__] and fails with "model is not fully defined" when
# that namespace is missing (measured the hard way on 11-06). Plain dicts are
# resolved by FastAPI from the function __globals__, where builtins always
# resolve -- solid.


def _brief(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: rec.get(k) for k in _BRIEF_KEYS}


def _load_or_404(job_id: str) -> Dict[str, Any]:
    rec = jobs.load(job_id)
    if rec is None:
        raise HTTPException(status_code=404,
                            detail=ui(f"作业不存在: {job_id}", f"job not found: {job_id}"))
    return rec


@router.get("/health")
def health() -> Dict[str, Any]:
    """Panel liveness probe + machine capability report: which ASR engines
    this machine actually has (drives the panel hint) and the merged ASR
    settings (engine/device/cpu cap)."""
    st = audio.asr_settings_view()
    return {"ok": True, "job_dir": str(jobs.JOB_DIR),
            "asr": audio.engines_available(),
            "asr_registered": st["registered"],
            "asr_settings": st}


@router.post("/asr/register")
def asr_register(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Register a transcription engine folder chosen through the panel's
    native folder dialog (the frontend sends the chosen path back).

    audio.register_asr_engine() recognizes the layout -- an R2T2 runtime
    root (worker.py + runtime/python/python.exe) or the root of a venv
    whose python can import faster_whisper -- then REALLY boots that
    engine and transcribes 0.2 s of silence (_verify_load): only a pass
    writes the registry and lights the circle green; anything else answers
    400 with a plain-language reason (which shape was expected, what the
    probe said) and registers nothing. The probe worker exits either way,
    so it leaves no VRAM/CPU behind. The pick survives gateway restarts
    (asr-engines.json); a first whisper run may download weights, so this
    call can take a while -- the panel shows a busy state, not a freeze."""
    body = payload or {}
    res = audio.register_asr_engine(str(body.get("path") or "").strip().strip('"'))
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail=res.get("reason") or ui("装载失败", "load failed"))
    return res


@router.post("/asr/cloud/test")
def asr_cloud_test(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Panel 「云端 ASR 测试连通」: GET {base}/v1/models (fills the model
    dropdown) and a REAL 0.2 s silence transcription through
    /v1/audio/transcriptions -- URL, key and audio endpoint proven
    together. Always 200: the verdict rides in the body, so a partial
    pass (models OK, audio dead) still reaches the panel instead of an
    HTTP error. An empty api_key falls back to the saved key; nothing is
    persisted here."""
    body = payload or {}
    return audio.test_cloud(body.get("base_url"), body.get("api_key"),
                            body.get("model"))


@router.post("/asr/cloud/save")
def asr_cloud_save(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist cloud ASR only after the same live transcription test
    passes; the cloud circle turns green on the next /health. A failed
    test answers 400 with the plain-language reason and writes nothing --
    a saved-but-dead cloud would lie green and fail every job."""
    body = payload or {}
    out = audio.save_cloud(body.get("base_url"), body.get("api_key"),
                           body.get("model"))
    if not out.get("ok"):
        raise HTTPException(status_code=400,
                            detail=out.get("reason") or ui("云端 ASR 验证没通过",
                                                           "cloud ASR verification failed"))
    return out


@router.post("/asr/settings")
def asr_settings_save(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist panel-side ASR settings (today: the CPU cap) without
    config.yaml or a gateway restart; unknown payloads fail loudly."""
    body = payload or {}
    if "cpu_percent" not in body:
        raise HTTPException(status_code=400,
                            detail=ui("payload 需要 cpu_percent",
                                      "payload needs cpu_percent"))
    out = audio.set_cpu_percent(body.get("cpu_percent"))
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason") or "bad cpu_percent")
    return out


@router.post("/asr/params/test")
def asr_params_test(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Live test of the engine parameters modal without persisting: the
    worker really boots with these knobs (device, quantization, model
    size, language...) and the engine's own failure words come back."""
    body = payload or {}
    out = audio.test_engine_params(body.get("engine"), body.get("params") or {})
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason") or "params test failed")
    return out


@router.post("/asr/params/save")
def asr_params_save(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist advanced engine parameters (R2T2 device/quant/language/
    hotwords, Whisper size/device/language/prompt). Save runs the same
    live load test as the test button first: knobs whose engine cannot
    actually boot on this box are rejected with the engine's own error,
    never saved as a trap for the next job."""
    body = payload or {}
    out = audio.save_engine_params(body.get("engine"), body.get("params") or {})
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason") or "params save failed")
    return out


@router.post("/asr/audio/save")
def asr_audio_save(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist the shared audio knobs (track index, channel, subtitle
    offset) that apply to every transcription engine. Data-level
    whitelist validation only — nothing boots here."""
    out = audio.save_audio_params(payload or {})
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason") or "audio save failed")
    return out


@router.get("/llm/probe")
def llm_probe() -> Dict[str, Any]:
    """Is the analysis LLM reachable? The panel gate calls this before
    starting any job (owner 2026-11-09: a dead engine must fail at the
    gate, not five seconds into a doomed job).

    The ping is llm.probe(): one tiny call through the very task slot
    real jobs use, via the host's official plugin LLM API -- same
    routing, same credentials, so probe green == job green. The host
    routes to the user's current chat model live, which is also what
    retires the 2026-11-10 stale-port incident: there is no plugin-side
    address left to go stale. model/provider report where the host
    actually routed; on failure the host's own one-line reason comes
    back so the UI shows what was unreachable.
    """
    return llm.probe()


@router.get("/asr/tracks")
def asr_audio_tracks(video: str = "") -> Dict[str, Any]:
    """How many audio streams one video carries — the 音轨 dropdown's
    max. ffprobe answers; a silent or missing file says so plainly."""
    out = audio.probe_tracks(video)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason") or "probe failed")
    return out


@router.get("/jobs")
def list_jobs(limit: int = 30) -> Dict[str, Any]:
    return {"jobs": [_brief(r) for r in jobs.list_jobs(limit=limit)]}


# --- Frame gallery -----------------------------------------------------
# The engine keeps frames in memory only (zero-disk pipeline), so the
# panel's "show me what was analysed" view re-samples on demand: every
# observation note carries its true frame timestamps ("[  12s] ..."), so
# asking ffmpeg for exactly those seconds reproduces what the model saw,
# frame for frame, without any persisted artifacts.

import re as _re

_NOTE_TS = _re.compile(r"^(?:[-*]\s*)?\[\s*(\d+:\d+(?::\d+)?)(?:\s*[-~]\s*\d+:\d+(?::\d+)?)?\s*\]")


def _mmss_to_s(t: str) -> float:
    parts = [float(x) for x in t.split(":")]
    s = 0.0
    for x in parts:
        s = s * 60 + x
    return s


def _gallery_extract(path: str, t_s: float):
    """One JPEG re-extracted at *t_s* seconds, or None on any failure.

    Same recipe as the sampler's 512px frames, so the gallery shows the
    pixels the model actually saw. Fail-soft: a missing/renamed video or
    a broken ffmpeg is a blank tile, never a 500.
    """
    try:
        from engine import sampling
        blob = sampling._run([sampling._ffmpeg(), "-v", "error",
                              "-ss", f"{max(0.0, t_s):.2f}", "-i", path,
                              "-vf", "scale=512:-2", "-frames:v", "1",
                              "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"])
    except Exception:
        return None
    if not blob or blob[:2] != bytes((0xFF, 0xD8)):
        return None
    return blob


def _gallery_frames(rec: Dict[str, Any]):
    """[(seg_index, [(t_s, label), ...]), ...] from the ledger notes.

    Each note = one observation segment; every "- [mm:ss-mm:ss] text"
    line is one observed frame. We take the window START as the frame's
    capture time (what the model actually saw at that instant). Lines
    without a timestamp bracket (prose, headers) are skipped.
    """
    out = []
    for si, note in enumerate(rec.get("notes") or []):
        frames = []
        for ln in str(note).splitlines():
            m = _NOTE_TS.match(ln.strip())
            if m:
                frames.append((_mmss_to_s(m.group(1)), ln.strip()))
        if frames:
            out.append((si, frames))
    return frames and out or out


@router.get("/jobs/gallery")
def gallery(id: str = Query(...), seg: int = Query(-1),
            frame: int = Query(-1)) -> Any:
    """Frame gallery for a finished/running job (on-demand, zero-disk).

    No seg → manifest {frames: [{seg, url, ts}]} — one url per observed
    frame, timestamps read from the ledger notes (exact times the model
    saw). seg+frame → that single frame re-sampled via ffmpeg at the
    recorded second, served as image/jpeg. Frame ids are ledger indices,
    so nothing here trusts a client-supplied path.
    """
    rec = _load_or_404(id.strip())
    manifest = _gallery_frames(rec)
    path = str((rec.get("params") or {}).get("path")
               or rec.get("path") or "")
    if seg < 0:
        # Thumbnails inline: one rest round-trip paints the whole gallery.
        import base64 as _b64
        frames = []
        for si, fl in manifest:
            for fi, (ts, _l) in enumerate(fl):
                blob = _gallery_extract(path, max(0.0, ts)) if path and os.path.isfile(path) else None
                if not blob:
                    continue
                frames.append({"seg": si, "i": fi, "ts": round(ts, 1),
                               "url": "/api/plugins/video-xray/jobs/gallery?id="
                                      f"{id}&seg={si}&frame={fi}",
                               "data_url": "data:image/jpeg;base64,"
                                           + _b64.b64encode(blob).decode("ascii")})
        return {"frames": frames}
    hit = next((f for si, frames in manifest if si == seg
                for fi, f in enumerate(frames) if fi == frame), None)
    if hit is None:
        raise HTTPException(status_code=404, detail=ui("帧不存在", "frame not found"))
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404,
                            detail=ui("视频文件已不在原位置", "video file no longer at its original path"))
    t = max(0.0, hit[0])
    blob = _gallery_extract(path, t)
    if blob is None:
        raise HTTPException(status_code=502, detail=ui(
            "取帧失败（ffmpeg 无输出或视频损坏）",
            "frame capture failed (empty or corrupt ffmpeg output)"))
    import base64 as _b64
    return {"data_url": "data:image/jpeg;base64," + _b64.b64encode(blob).decode("ascii"),
            "ts": round(t, 1)}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str) -> Dict[str, Any]:
    rec = _load_or_404(job_id)
    out = _brief(rec)
    out["params"] = rec.get("params") or {}
    out["notes"] = rec.get("notes") or []          # each item = one segment's multi-line [mm:ss-mm:ss] text
    out["summary"] = rec.get("summary")
    out["transcript"] = rec.get("transcript")      # data source for the dialogue pane
    out["log"] = rec.get("log")
    return out


def _num(body: Dict[str, Any], key: str, cast, lo=None, hi=None, default=None):
    """Narrow validation of numeric panel inputs: empty string/None = use
    the default; out of range = 400 (never feed garbage to the engine)."""
    v = body.get(key, default)
    if v is None or v == "":
        return default
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=ui(
            f"{key} 得是数字，收到 {v!r}", f"{key} must be a number, got {v!r}"))
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise HTTPException(status_code=400, detail=ui(
            f"{key} 需在 {lo}-{hi} 之间", f"{key} must be between {lo} and {hi}"))
    return x


_ASR_CHOICES = ("auto", "r2t2", "whisper", "cloud", "off")


def _asr_choice(v) -> str:
    """The transcribe engine knob from panel/API payloads: engine name, or
    legacy bool (True -> auto, False -> off). Anything else = 400 so typos
    never silently degrade into a different engine."""
    if v is True or v is None or v == "":
        return "auto"
    if v is False:
        return "off"
    s = str(v).strip().lower()
    if s not in _ASR_CHOICES:
        raise HTTPException(status_code=400, detail=ui(
            "transcribe 只能是 " + "/".join(_ASR_CHOICES) + "（或 true/false）",
            "transcribe must be one of " + "/".join(_ASR_CHOICES) +
            " (or true/false)"))
    return s


@router.post("/analyze")
def start_analyze(body: dict) -> Dict[str, Any]:
    """Start an analysis; returns the job id immediately (analysis runs on a
    background thread inside the gateway process).

    Panel parameters: path / preset(default|quick8|single32|single64|
    segment24fps|segment30fps) / max_frames(8-64) / start / duration
    (converted to the engine's end) / strategy(uniform|adaptive) /
    subtitles(pasted subtitle text) / transcribe(auto|r2t2|whisper|cloud|off,
    default auto; legacy true/false still accepted).
    fps and max_frames empty = auto per preset; duration empty = to the end
    of the video."""
    path = str(body.get("path") or "").strip()
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=400, detail=ui(
            f"文件不存在: {path or '(没填 path)'}",
            f"file not found: {path or '(no path given)'}"))
    # Presets follow the engine SCHEMA_ANALYZE English keys (checked 11-06:
    # the engine has no "deep").
    preset = str(body.get("preset") or "default").strip()
    if preset not in _PRESETS:
        raise HTTPException(status_code=400, detail=ui(
            "preset 只能是 ", "preset must be one of ") + "/".join(_PRESETS))
    fps = _num(body, "fps", float, 0.05, 10.0)
    max_frames = _num(body, "max_frames", int, 8, 64)
    start = _num(body, "start", float, 0.0, None, 0.0) or 0.0
    duration = _num(body, "duration", float, 0.1, None)
    strategy = str(body.get("strategy") or "").strip()
    if strategy and strategy not in ("uniform", "adaptive"):
        raise HTTPException(status_code=400, detail=ui(
            "strategy 只能是 uniform/adaptive",
            "strategy must be uniform/adaptive"))
    # Pasted subtitles (SRT/VTT/timestamped lines) make the engine skip ASR
    # entirely -- the panel's "paste dialogue" box.
    subtitles = str(body.get("subtitles") or "").strip()
    if len(subtitles) > 20000:
        subtitles = subtitles[:20000]
    params: Dict[str, Any] = {
        "path": path,
        "preset": preset,
        "transcribe": _asr_choice(body.get("transcribe", "auto")),
        "start": start,
    }
    if strategy:
        params["strategy"] = strategy
    if subtitles:
        params["subtitles"] = subtitles
    if fps:
        params["fps"] = fps
    if max_frames:
        params["max_frames"] = max_frames
    if duration:
        params["end"] = start + duration
    rec = jobs.start_bg(params)                    # returns at once; analysis runs on a background thread
    return {"id": rec["id"], "status": rec["status"]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    _load_or_404(job_id)
    rec = jobs.cancel(job_id)
    return {"ok": True, "status": (rec or {}).get("status"),
            "cancel_requested": True}


@router.post("/jobs/delete")
def delete_jobs(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Delete exactly the rows ticked in the panel. Running ones are refused;
    the reasons ride back in `refused` for display.

    POST rather than DELETE: the current desktop shell's plugin rest only
    allows GET/POST, so no Electron main-process patch is needed and an
    official upgrade can never undo one."""
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail=ui(
            "没说要删哪几条（ids 为空）", "no job ids given (ids is empty)"))
    if len(ids) > 100:
        raise HTTPException(status_code=400,
                            detail=ui("一次最多删 100 条", "at most 100 ids per call"))
    return jobs.delete_many([str(i) for i in ids])

@router.post("/jobs/clear")
def clear_jobs() -> Dict[str, Any]:
    # One-click cleanup of every ended job; running/queued ones are skipped, never swallowed
    return jobs.clear_ended()

@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str) -> Dict[str, Any]:
    """Background resume (reuses completed segments); returns the new job id
    immediately."""
    rec, err = jobs.resume(job_id, bg=True)
    if rec is None:
        raise HTTPException(status_code=400, detail=err)
    if err == "already_done":
        return {"id": job_id, "status": rec.get("status"), "note": "already_done"}
    return {"id": rec["id"], "status": rec["status"], "resume_of": job_id}

def _h3_opts(payload):
    """H3 export options from the green bar: mode=single|shots, thresh=shot-cut detection threshold."""
    body = payload if isinstance(payload, dict) else {}
    mode = "single" if str(body.get("mode", "shots")).strip().lower() == "single" else "shots"
    try:
        thresh = float(body.get("thresh", h3prompt.DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=ui(
            "镜头阈值得是个数（0.05~0.95）",
            "the shot threshold must be a number (0.05-0.95)"))
    if not h3prompt.THRESH_MIN <= thresh <= h3prompt.THRESH_MAX:
        raise HTTPException(status_code=400, detail=ui(
            f"镜头阈值得在 {h3prompt.THRESH_MIN}~{h3prompt.THRESH_MAX} 之间",
            f"the shot threshold must be between {h3prompt.THRESH_MIN} and {h3prompt.THRESH_MAX}"))
    return mode, thresh


@router.post("/jobs/{job_id}/h3")
def h3_prompt(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse-build this job into a MiniMax-H3 shot prompt; returns the
    text for copy/preview.

    Sync def: LLM assembly takes seconds, so FastAPI automatically shifts
    this onto its thread pool -- the gateway event loop never blocks."""
    mode, thresh = _h3_opts(payload)
    r = h3prompt.build(_load_or_404(job_id), mode=mode, thresh=thresh,
                       use_llm=bool((payload or {}).get("llm", True)))
    if not r.get("h3"):
        raise HTTPException(status_code=409, detail=r.get("warning") or ui(
            "这条作业没有观察记录，反推不了",
            "this job has no observation notes; cannot build a prompt"))
    return {"ok": True, **r}


@router.post("/jobs/{job_id}/export")
def export_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save-As asks only once (the frontend picks the location and sends it
    here); this writes two files in one shot: the chosen path gets the .md
    analysis report, and a sibling `<stem>_H3_prompt.txt` is derived next to
    it."""
    body = payload if isinstance(payload, dict) else {}
    raw = str(body.get("path") or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400,
                            detail=ui("没给保存路径", "no save path given"))
    mode, thresh = _h3_opts(payload)
    r = h3prompt.build(_load_or_404(job_id), mode=mode, thresh=thresh,
                       use_llm=bool(body.get("llm", True)))
    if not r.get("h3"):
        raise HTTPException(status_code=409, detail=r.get("warning") or ui(
            "这条作业没有观察记录，导不出",
            "this job has no observation notes; cannot export"))
    target = Path(raw)
    if target.suffix.lower() not in (".md", ".markdown", ".txt"):
        target = target.with_suffix(".md")
    txt = target.with_name(target.stem + ui("_H3提示词.txt", "_H3_prompt.txt"))
    if txt == target:
        txt = target.with_name(target.stem + "_H3.txt")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(r["md"], encoding="utf-8")
        txt.write_text(r["h3"] + "\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=ui(f"写盘失败: {exc}", f"write failed: {exc}")) from exc
    return {"ok": True, "md_path": str(target), "txt_path": str(txt),
            "shots": r.get("shots"), "engine": r.get("engine"),
            "mode": r.get("mode"), "thresh": r.get("thresh"), "warning": r.get("warning")}
