"""video-xray plugin shell: registers chat tools + the /video command.

Main chat path (single one): the video_xray tool runs to completion
blocking-style and returns the full report in one shot.
/video <path> uses a background thread + file ledger for progress;
list/cancel/resume manage jobs.
"""
import json
import logging
import os
import sys

log = logging.getLogger(__name__)

try:                                   # in-package load (gateway) first; loose script fallback
    from .engine import jobs, llm, pipeline
except ImportError:                    # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from engine import jobs, llm, pipeline

ui = llm.ui

_PRESETS = ["quick8", "single32", "single64", "segment24fps",
            "segment30fps", "default", "快速概览·8帧", "单次观察·32帧",
            "单次观察·64帧", "全程分段·24 FPS", "全程分段·30 FPS"]

SCHEMA_ANALYZE = {
    "name": "video_xray",
    "description": (
        "Analyze the content of a video file (frame segments observed by the "
        "local vision engine + optional speech transcription) and return a "
        "timestamped full report. Runs synchronously: ~1-3 min for short "
        "videos, longer for long ones; the tool call stays pending meanwhile. "
        "preset: quick8 overview / default segmented (fps8) / segment24fps "
        "full-span segmented / single32|64 one-shot. Optional start/end "
        "seconds, strategy=adaptive to densify around change points, "
        "subtitles to inject ready-made captions (SRT text), "
        "transcribe picks the ASR engine (auto / r2t2 / whisper / cloud / off). "
        "分析视频内容（分段观察+可选语音转录），返回带时间戳的完整报告。"),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "absolute path of the video file"},
            "preset": {"type": "string", "enum": _PRESETS,
                       "description": "sampling preset, default=default (segmented fps8)"},
            "fps": {"type": "number", "description": "frames per second (overrides preset)"},
            "max_frames": {"type": "integer", "description":
                           "single-pass: total frame cap; segment mode: frames "
                           "per segment (total = per-seg x 4 observations)"},
            "start": {"type": "number", "description": "start second, default 0"},
            "end": {"type": "number", "description": "end second, default whole video"},
            "strategy": {"type": "string", "enum": ["uniform", "adaptive"]},
            "subtitles": {"type": "string",
                          "description": "ready-made caption text (SRT/timed lines); "
                                         "injecting it skips ASR"},
            "transcribe": {"type": "string",
                           "enum": ["auto", "r2t2", "whisper", "cloud", "off"],
                           "description": "ASR engine for dialogue: auto = whatever this "
                                          "machine has (ladder R2T2 -> Whisper -> cloud, "
                                          "the default); r2t2 / whisper / cloud = force one "
                                          "engine; off = video only. Past subtitles always "
                                          "inject; videos without an audio track skip ASR"},
        },
        "required": ["path"],
    },
}

SCHEMA_JOBS = {
    "name": "video_xray_jobs",
    "description": ("Manage video analysis jobs: action=list recent jobs / "
                    "status check one job's progress / cancel (keeps completed "
                    "segments) / resume continues after an interruption or "
                    "gateway restart. 管理视频分析作业。"),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["list", "status", "cancel", "resume"]},
            "job_id": {"type": "string"},
        },
        "required": ["action"],
    },
}


def _clean_params(args):
    params = {"path": str(args.get("path", "")).strip().strip('"')}
    for k in ("preset", "strategy", "subtitles"):
        if args.get(k):
            params[k] = args[k]
    for k in ("fps", "start", "end"):
        if args.get(k) is not None:
            params[k] = float(args[k])
    if args.get("max_frames") is not None:
        params["max_frames"] = int(args["max_frames"])
    # Panel/chat not passing the key at all = defer to config.yaml /
    # VA_ASR_TRANSCRIBE, then the ladder (the panel's 保存 must actually
    # change future tool calls). Explicit values win; legacy booleans from
    # old callers stay accepted: True -> auto, False -> off.
    tv = args.get("transcribe")
    if tv is not None:
        params["transcribe"] = ("auto" if tv is True else "off" if tv is False
                                else (str(tv).strip().lower() or "auto"))
    return params


def _report(job):
    st = job.get("status")
    head = (f"[video_xray job={job['id']} status={st} "
            f"segments={job.get('done')}/{job.get('total')} "
            f"transcript={'yes' if job.get('transcript_used') else 'no'} "
            f"elapsed={job.get('elapsed_s', '-')}s]")
    body = job.get("summary") or job.get("error") or (job.get("log") or "")[-400:]
    tail = "" if st == "done" else ui(
        "\n(可用 video_xray_jobs resume 续跑，参数一致)",
        "\n(you can continue with video_xray_jobs action=resume; same parameters)")
    return f"{head}\n{body}{tail}"


def _handle_analyze(args, **_kw):
    args = args or {}
    rid = str(args.get("resume_of", "")).strip()
    if rid:
        # resume back into the engine: reuse the old job's completed segments +
        # transcription, only fill the gap (the engine validates size/mtime)
        rec, err = jobs.resume(rid)
        if rec is None:
            return ui(f"[video_xray error] 续跑失败: {err}",
                      f"[video_xray error] resume failed: {err}")
        if err == "already_done":
            return _report(rec) + ui(
                "\n(该作业已完成，无待补段；要重跑就别带 resume_of)",
                "\n(this job already finished, nothing to fill in; to re-run, "
                "drop resume_of)")
        return _report(rec)
    params = _clean_params(args)
    if not os.path.isfile(params["path"]):
        return ui(f"[video_xray error] 文件不存在: {params['path']}",
                  f"[video_xray error] file not found: {params['path']}")
    return _report(jobs.run_sync(params))


def _handle_jobs(args, **_kw):
    action = str((args or {}).get("action", "list"))
    job_id = str((args or {}).get("job_id", "")).strip()
    if action == "list":
        recs = jobs.list_jobs()
        if not recs:
            return ui("暂无分析作业", "no analysis jobs yet")
        return "\n".join(
            f"{r['id']}  {r['status']:<9} {r['done']}/{r['total']}  "
            f"{r['created_at']}  {os.path.basename(r['path'])}" for r in recs)
    rec = jobs.load(job_id)
    if action == "status":
        return _report(rec) if rec else ui(f"作业不存在: {job_id}",
                                           f"job not found: {job_id}")
    if action == "cancel":
        rec = jobs.cancel(job_id)
        return (ui(f"已请求取消 {job_id}（已完成段保留）",
                   f"cancellation requested for {job_id} (completed segments kept)")
                if rec else ui(f"作业不存在: {job_id}", f"job not found: {job_id}"))
    if action == "resume":
        rec, err = jobs.resume(job_id)
        if err == "already_done":
            return ui(f"作业 {job_id} 早已完成。\n",
                      f"job {job_id} finished long ago.\n") + _report(rec)
        return _report(rec) if rec else f"[resume] {err or ui('失败', 'failed')}"
    return ui(f"未知 action: {action}", f"unknown action: {action}")


def _slash_command(raw_args: str) -> str:
    raw = (raw_args or "").strip()
    if not raw:
        return ui("用法: /video <视频路径> [preset] | /video list | "
                  "/video status <id> | /video cancel <id> | /video resume <id>",
                  "usage: /video <path> [preset] | /video list | "
                  "/video status <id> | /video cancel <id> | /video resume <id>")
    parts = raw.split()
    if parts[0] in ("list", "status", "cancel", "resume"):
        if parts[0] == "list":
            return _handle_jobs({"action": "list"})
        return _handle_jobs({"action": parts[0],
                             "job_id": parts[1] if len(parts) > 1 else ""})
    path = parts[0]
    if not os.path.isfile(path):
        return ui(f"文件不存在: {path}", f"file not found: {path}")
    params = {"path": path}
    rest = parts[1:]
    # First bare token = preset; then key=value tokens for the knobs the
    # panel exposes too (transcription on/off, window slicing).
    if rest and "=" not in rest[0]:
        params["preset"] = rest[0]
        rest = rest[1:]
    for tok in rest:
        key, _, val = tok.partition("=")
        try:
            if key == "transcribe":
                params["transcribe"] = val.strip().lower()
            elif key == "start":
                params["start"] = float(val)
            elif key == "end":
                params["end"] = float(val)
        except ValueError:
            return ui(f"参数格式不对: {tok}（start/end 需用秒数）",
                      f"bad argument: {tok} (start/end are seconds)")
    job = jobs.start_bg(params)
    return ui(f"分析已在后台启动: job={job['id']}\n"
              "进度: /video status " + job["id"] + "（完成后可直接问我要报告）",
              f"analysis started in the background: job={job['id']}\n"
              "progress: /video status " + job["id"] +
              " (once done, just ask me for the report)")


def register(ctx):
    ctx.register_tool(name="video_xray", toolset="video_xray",
                      schema=SCHEMA_ANALYZE, handler=_handle_analyze,
                      description="video content analysis (segmented observation + speech transcription)",
                      emoji="🎬")
    ctx.register_tool(name="video_xray_jobs", toolset="video_xray",
                      schema=SCHEMA_JOBS, handler=_handle_jobs,
                      description="video analysis job management (list/status/cancel/resume)")
    ctx.register_command(
        "video", handler=_slash_command,
        description="video analysis: /video <path> [preset] | list | status | cancel | resume",
        args_hint="<video>|list|status|cancel|resume")
    # Official auxiliary-task registration: the analysis engine calls
    # agent.auxiliary_client.call_llm(task="video_xray") and follows the
    # main chat model by default; this row makes the task appear in the
    # desktop auxiliary config panel, where a user can pin a dedicated
    # provider/model for video work (auxiliary.video_xray.* in config.yaml).
    # Hosts without this API are not fatal: call_llm routes an unknown task
    # to the main model anyway.
    try:
        ctx.register_auxiliary_task(
            "video_xray",
            display_name="Video analysis (video-xray)",
            description="frame observation, note compression and the final report "
                        "of the video-xray plugin; unset = follows the main chat model",
        )
    except (AttributeError, TypeError) as e:
        log.info("auxiliary task registration skipped (%s); call_llm still "
                 "routes the task to the main model", e)
