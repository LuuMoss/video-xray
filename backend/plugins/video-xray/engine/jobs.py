"""Job ledger: xray-jobs/<id>.json state files + cancel flags + resume.

Analysis runs in threads inside the gateway process; every finished
segment is persisted atomically as it lands -- a gateway restart never
loses completed segments, resume reuses them. Cancel = in-memory event
OR the <id>.cancel file (works across processes).
"""
import itertools
import json
import os
import threading
import time
from pathlib import Path

from . import llm, pipeline

ui = llm.ui

_HERMES_HOME = Path(os.environ.get(
    "HERMES_HOME", str(Path(__file__).resolve().parents[3])))
JOB_DIR = _HERMES_HOME / "xray-jobs"
_ACTIVE: dict = {}   # id -> {"stop": Event}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _asr_param(val):
    """Ledgers written before 0.1 stored transcribe as a boolean switch;
    normalize any stored value to an engine name for the pipeline."""
    if isinstance(val, bool):
        return "auto" if val else "off"
    return str(val or "auto")


def _file(job_id):
    return JOB_DIR / f"{job_id}.json"


_tmp_seq = itertools.count()


def _save(job):
    """Atomic write. The temp name must be unique every time: on Windows a
    Python file open uses exclusive-share mode, so if the worker thread
    saving progress and the cancel/HTTP thread write the same fixed
    <id>.tmp name concurrently, one gets PermissionError (measured 11-06:
    this is exactly what blew up on cancel). On a same-name clash, back off,
    retry under a new name."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    dst = _file(job["id"])
    err = None
    for i in range(6):
        tmp = JOB_DIR / ("%s.%d.%d.%d.tmp" % (
            job["id"], os.getpid(), threading.get_ident(), next(_tmp_seq)))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=1)
            os.replace(tmp, dst)
            return
        except OSError as e:
            err = e
            try:
                os.unlink(tmp)
            except OSError:
                pass
            time.sleep(0.04 * (i + 1))
    raise err


def _sweep_orphans():
    """After a gateway restart / process exit, 'running' records are orphans
    (their threads died with the old process) -> mark them interrupted and
    resumable. Rule = status running and id not in this process's _ACTIVE;
    same for pending, but with a 60 s grace (a job started in this process
    is legitimately pending for a few tens of milliseconds -- do not
    misjudge it)."""
    if not JOB_DIR.is_dir():
        return
    # Temp files left by a crash mid-write: anything older than 10 minutes
    # gets purged (an atomic write never leaves a .tmp behind).
    cutoff = time.time() - 600
    for p in JOB_DIR.glob("*.tmp"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    for p in JOB_DIR.glob("*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        st = rec.get("status")
        if st not in ("running", "pending") or rec.get("id") in _ACTIVE:
            continue
        # pending is only legal for the few tens of ms after this process
        # starts a job (the thread has not flipped it to running yet); still
        # pending after 60 s = the process that created it died -> orphan,
        # mark resumable.
        if st == "pending" and time.time() - rec.get("created_ts", 0) < 60:
            continue
        rec["status"] = "interrupted"
        rec["interrupted_at"] = _now()
        _save(rec)


def load(job_id):
    try:
        with open(_file(job_id), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if rec.get("status") == "running" and rec.get("id") not in _ACTIVE:
        rec["status"] = "interrupted"
        rec["interrupted_at"] = _now()
        _save(rec)
    return rec


def list_jobs(limit=20):
    _sweep_orphans()
    if not JOB_DIR.is_dir():
        return []
    recs = []
    for p in JOB_DIR.glob("*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                recs.append(json.load(f))
        except (OSError, ValueError):
            continue
    recs.sort(key=lambda r: r.get("created_ts", 0), reverse=True)
    return recs[:limit]


def _new_id(path):
    stem = "".join(c for c in Path(path).stem if c.isalnum() or c in "-_")[:40]
    return f"{stem}-{time.strftime('%m%d-%H%M%S')}"


def _run(job, params, resume=None):
    ev = threading.Event()
    _ACTIVE[job["id"]] = {"stop": ev}
    cancel_file = JOB_DIR / f"{job['id']}.cancel"
    cancel_file.unlink(missing_ok=True)   # stale flag: cleared at start; during a run it means cancel

    def should_stop():
        return ev.is_set() or cancel_file.exists()

    def on_progress(msg):
        job["log"] = ((job.get("log") or "") + "\n" + msg)[-1600:]
        _save(job)

    def on_note(notes, done, total):
        job["done"], job["total"] = done, total
        job["notes"] = notes
        _save(job)

    job.update(status="running", started_at=_now())
    _save(job)
    try:
        res = pipeline.analyze(
            params["path"], preset=params.get("preset", "default"),
            mode=params.get("mode"), fps=params.get("fps"),
            max_frames=params.get("max_frames"),
            start=params.get("start", 0.0), end=params.get("end"),
            strategy=params.get("strategy", "uniform"),
            subtitles=params.get("subtitles"),
            transcribe=_asr_param(params.get("transcribe")),
            on_progress=on_progress, should_stop=should_stop,
            resume=resume, on_note=on_note)
        job.update(summary=res["summary"], notes=res["notes"],
                   summary_truncated=res.get("summary_truncated"),
                   done=res["segments_done"], total=res["total"],
                   transcript_used=res["transcript_used"],
                   transcript_engine=res.get("transcript_engine"),
                   transcript=res.get("transcript"),
                   elapsed_s=res["elapsed_s"],
                   status="cancelled" if res["stopped_early"] else "done")
    except Exception as e:                                  # noqa: BLE001
        job.update(status="error", error=f"{type(e).__name__}: {e}"[:500])
    finally:
        _ACTIVE.pop(job["id"], None)
        cancel_file.unlink(missing_ok=True)
        job["finished_at"] = _now()
        _save(job)
    return job


def run_sync(params, resume=None):
    """Run to completion on the calling thread (chat-tool path: one call,
    full report)."""
    job = _make(params)
    return _run(job, params, resume)


def start_bg(params):
    """Run on a background thread (/video command path: returns at once,
    progress visible in the ledger)."""
    job = _make(params)
    _save(job)   # persist the ledger entry before spawning the thread: whoever got the id (panel/HTTP) can query it immediately, never a 404
    threading.Thread(target=_run, args=(job, params), daemon=True).start()
    return job


def _make(params):
    st = os.stat(params["path"])
    return {"id": _new_id(params["path"]), "path": params["path"],
            "params": params, "status": "pending", "done": 0, "total": 0,
            "notes": [], "summary": None, "error": None, "log": "",
            "created_at": _now(), "created_ts": time.time(),
            "size": st.st_size, "mtime": int(st.st_mtime)}


def delete(job_id):
    """Manually delete one ledger entry (record + its cancel flag);
    **ended jobs only**.

    Running jobs are blocked at the panel/command layer; this is the safety
    net underneath: refuse while active, so the ledger never disappears
    under a thread still calling _save()."""
    if _ACTIVE.get(job_id) is not None:
        return False, ui("这条正在分析中，先取消再删",
                         "this job is still analyzing; cancel it first")
    for p in [JOB_DIR / ("%s.json" % job_id), JOB_DIR / ("%s.cancel" % job_id),
              *JOB_DIR.glob("%s.*.tmp" % job_id)]:       # sweep write junk left by a crash-restart too
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            return False, ui("删除失败: %s" % exc, "delete failed: %s" % exc)
    return True, ""


def delete_many(ids):
    """Delete exactly the rows the panel ticked: each goes through delete()
    (single path, one rule, no drift); refusal reasons for running jobs are
    carried back verbatim for display. Returns {deleted: [...], refused:
    [{id, reason}]}."""
    deleted, refused = [], []
    for jid in ids:
        ok, err = delete(jid)
        if ok:
            deleted.append(jid)
        else:
            refused.append({"id": jid, "reason": err})
    return {"deleted": deleted, "refused": refused}


def clear_ended():
    """One-click cleanup: **deletes only ended entries**
    (done/cancelled/error/interrupted); running/pending are skipped and
    listed in the reply -- pressing clear never swallows a live job.

    Disk is the source of truth (same stance as _sweep_orphans), not
    in-memory state."""
    if not JOB_DIR.is_dir():
        return {"deleted": 0, "skipped_running": [], "failed": []}
    deleted, skipped, failed = 0, [], []
    for p in sorted(JOB_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = None
        jid = (rec or {}).get("id") or p.stem
        st = (rec or {}).get("status")
        if st in ("running", "pending") or _ACTIVE.get(jid) is not None:
            skipped.append(jid)
            continue
        try:
            p.unlink()
            deleted += 1
        except OSError:
            failed.append(jid)
        for extra in JOB_DIR.glob("%s.*" % jid):        # cancel flags + crash-leftover tmps
            if extra.suffix in (".cancel", ".tmp"):
                try:
                    extra.unlink()
                except OSError:
                    pass
    for c in JOB_DIR.glob("*.cancel"):                   # orphan cancel flags (no ledger)
        try:
            c.unlink()
        except OSError:
            pass
    return {"deleted": deleted, "skipped_running": skipped, "failed": failed}


def cancel(job_id):
    """Belt and braces: in-memory event + cancel file (a cross-process
    worker gets it too)."""
    a = _ACTIVE.get(job_id)
    if a:
        a["stop"].set()
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    (JOB_DIR / f"{job_id}.cancel").touch()
    rec = load(job_id)
    if rec and rec["status"] not in ("done", "error"):
        rec["cancel_requested"] = True
        _save(rec)
    return rec


def resume(job_id, stop_after=None, bg=False):
    """Resume from the breakpoint: completed segments are reused. A changed
    video file is refused (the frame plan would misalign).
    stop_after: test hook, stop after n segments.
    bg=True: run on a background thread, return the new job ledger at once
    (panel/HTTP use this -- never block the request until analysis ends).
    Returns (rec, err): rec=None = refused; err="already_done" = nothing to
    finish."""
    rec = load(job_id)
    if not rec:
        return None, ui("作业不存在", "job not found")
    if rec["status"] == "running" and job_id in _ACTIVE:
        return None, ui("作业正在本进程运行中，无需续跑",
                        "the job is already running in this process; nothing to resume")
    try:
        st = os.stat(rec["path"])
    except OSError:
        return None, ui(f"视频文件已不存在: {rec['path']}",
                        f"video file no longer exists: {rec['path']}")
    if st.st_size != rec.get("size") or int(st.st_mtime) != rec.get("mtime"):
        return None, ui("视频文件已被改动，断点会错位；请重新发起分析",
                        "the video file changed; breakpoint resume would misalign — start a fresh analysis")
    if rec.get("done", 0) >= rec.get("total", 0) and rec["status"] == "done":
        return rec, "already_done"
    rec["status"] = "pending"
    _save(rec)
    base = {"done": rec["done"], "notes": rec["notes"]}
    job = _make(rec["params"])          # new id, so every resume is traceable in the ledger
    job["resume_of"] = job_id
    if stop_after is not None:
        cnt = {"n": 0}
        orig = pipeline.analyze

        def limited(*a, **k):
            entry = _ACTIVE.setdefault(job["id"], {"stop": threading.Event()})
            ev = entry["stop"]          # the object _run's should_stop checks
            inner = k.pop("on_note", None)

            def on_note(notes, done, total):
                if inner:
                    inner(notes, done, total)
                cnt["n"] += 1
                if cnt["n"] >= stop_after:
                    ev.set()
            k["on_note"] = on_note
            return orig(*a, **k)
        pipeline.analyze = limited
    def go():
        try:
            _run(job, rec["params"], resume=base)
        finally:
            if stop_after is not None:
                pipeline.analyze = orig
    if bg:
        _save(job)   # same rule: persist the new ledger entry first, the panel can query the id immediately
        threading.Thread(target=go, daemon=True).start()
        return job, None
    go()
    return load(job["id"]), None
