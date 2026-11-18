"""Job engine: preset -> frame sampling -> segmented observation ->
compression -> summary (audio timeline included).

Aligned with the Strata contract: observation uses temperature:0 and
explicitly requires first/last frame comparison; transcript text over 6000
chars is group-compressed first; only the final summary uses chat sampling;
cancel/failure keeps the completed segment count and never claims the whole
video finished. Hard cap 512 segments. Single path: chat tools and the UI
both call analyze().

User-visible strings go through llm.ui(zh, en) and follow the system language
(config display.language), same source as the panel i18n.
"""
import time

from . import llm, sampling
from .llm import ui

MAX_SEGMENTS = 512
COMPRESS_AT_CHARS = 6000
SEGMENT_TARGET = 4               # observation requests per video (constant cost)
MAX_FRAMES_PER_SEG = 16          # legacy fixed per-segment size (pre 11-08 behaviour)

PRESETS = {
    "quick8":       dict(mode="once",    max_frames=8),
    "single32":     dict(mode="once",    max_frames=32),
    "single64":     dict(mode="once",    max_frames=64),
    "segment24fps": dict(mode="segment", fps=24),
    "segment30fps": dict(mode="segment", fps=30),
    "default":      dict(mode="segment", fps=8),
}
PRESET_ALIASES = {
    "快速概览·8帧": "quick8", "单次观察·32帧": "single32", "单次观察·64帧": "single64",
    "全程分段·24 FPS": "segment24fps", "全程分段·30 FPS": "segment30fps",
    "均匀覆盖(默认)": "default", "均匀覆盖（默认）": "default",
}


def resolve_preset(name):
    key = PRESET_ALIASES.get(str(name).strip(), str(name).strip())
    return dict(PRESETS.get(key, PRESETS["default"]))


def _ts(t):
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def _chunks(seq, n):
    """Split into n equal-count groups (used by compression)."""
    k = max(1, -(-len(seq) // n))
    return [seq[i:i + k] for i in range(0, len(seq), k)]


def _by_size(seq, size):
    """Split into blocks of `size` items (used for segments)."""
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _observe(frames, seg_no, seg_total, prev_note, span):
    """One-segment observation: temp 0, describe the current segment only,
    explicitly require first/last frame comparison."""
    parts = [llm.text_part(
        ui(
            f"这是视频 {span} 段（第 {seg_no}/{seg_total} 段）按时间顺序抽取的帧，"
            f"每帧前有 [mm:ss] 时间戳。只描述**当前片段**：主体、动作、场景变化、"
            f"镜头运动。必须比较首尾两帧——若画面基本不变要如实说明，别编造运动。",
            f"These frames were sampled in time order from the video span "
            f"{span} (segment {seg_no}/{seg_total}); each frame is preceded "
            f"by its [mm:ss] timestamp. Describe **only this segment**: "
            f"subjects, actions, scene changes, camera motion. You must "
            f"compare the first and last frames -- if the picture is "
            f"essentially unchanged, say so honestly; do not invent motion.",
        )
        + (ui(f"\n上一段结尾记录（仅供开头衔接，不要重复它）：{prev_note[-400:]}",
              f"\nPrevious segment's ending note (for continuity only; do not "
              f"repeat it): {prev_note[-400:]}") if prev_note else "")
        + ui("\n输出：3-6 行要点，每行带时间范围。",
             "\nOutput: 3-6 bullet lines, each with a time range."))]
    for t, jpg in frames:
        parts.append(llm.text_part(f"[{_ts(t)}]"))
        parts.append(llm.image_part(jpg))
    budget = 800 + 40 * len(frames)   # thinking model: a small budget is all eaten by thinking
    content, finish, _u = llm.chat(parts, temperature=0.0,
                                   max_tokens=min(3000, budget))
    if not content.strip():
        raise RuntimeError(ui(f"第 {seg_no} 段观察为空(finish={finish})",
                              f"segment {seg_no} observation is empty "
                              f"(finish={finish})"))
    if finish == "length":
        # Observation is perception + frame comparison; keep thinking (only
        # with thinking does it write conclusions like "fixed camera, first
        # and last agree"), and thinking eats max_tokens, so the budget can
        # only be estimated by frame count. When capped, say so in the note
        # -- silent degradation would lower report quality with nobody the
        # wiser (11-06 measured: 16 frames with thinking used 748/1440
        # tokens, plenty of headroom; only many frames hit the cap).
        content += ui(f"\n[注：本段观察在 {min(3000, budget)} token 处被截断]",
                      f"\n[Note: this segment's observation was truncated at "
                      f"{min(3000, budget)} tokens]")
    return content.strip()


def _compress(notes, on_progress=None):
    """When transcript text exceeds 6000 chars, group-compress (4 segments
    per group folded into timeline bullets)."""
    if sum(len(n) for n in notes) <= COMPRESS_AT_CHARS:
        return list(notes)
    out = []
    groups = _by_size(notes, 4)
    for gi, g in enumerate(groups, 1):
        on_progress and on_progress(
            ui(f"压缩记录 {gi}/{len(groups)}", f"compressing notes {gi}/{len(groups)}"))
        joined = "\n---\n".join(g)
        content, _f, _u = llm.chat(
            [llm.text_part(
                ui("下面是多段视频观察记录。合并压缩成一份时间线要点，"
                   "保留所有具体事件/动作/外观/台词条目与时间戳，"
                   "删除重复与客套措辞，不要新增推断：\n\n",
                   "Below are observation notes from several video segments. "
                   "Merge and compress them into one timeline of bullets, "
                   "keeping every concrete event/action/appearance/dialogue "
                   "entry and timestamp, removing repetition and filler "
                   "phrasing, adding no new inference:\n\n") + joined)],
            temperature=0.0, max_tokens=1600, reasoning_effort="none")
        # Compression is "fold notes into notes" -- no reasoning; thinking
        # would eat most of that 1600, and a cut tail means lost notes: on
        # truncation fall back to the original text; failed compression is
        # worse than none (11-06).
        out.append((joined if _f == "length" else content.strip()) or joined)
    return out


def _summary_budget(notes, transcript):
    """Summary budget scales with segment count: a thinking model spends part
    of max_tokens on reasoning, and a fixed 2500 cut report endings on
    multi-segment jobs (11-06 measured: 4 segments broke mid-sentence).
    2500=thinking reserve (v0.3.3: Flash-Next class models reason far
    heavier than the 11-06 calibration; a 4-segment job truncated inside
    a 1500 reserve), 800=report skeleton, 1000/segment=timeline body,
    600=dialogue area when a transcript exists, 24000=hard ceiling
    (long jobs go two-level via _summarize_long instead of pressing the
    ceiling — see _LONG_SEGMENTS)."""
    return max(2500, min(2500 + 800 + 1000 * max(1, len(notes))
                          + (600 if transcript else 0), 24000))


_LONG_SEGMENTS = 20     # past this a single pass presses the ceiling
# 11-19 主人 35min 粗化预设案：4 段×9 分钟(转录 12K 字)照样吃穿单层
# 预算——段数少但每段巨大同样压顶。笔记+转录合计超此字数也走两级。
_LONG_INPUT_CHARS = 8000  # notes+transcript chars past which we go two-level
_CHAPTER_SEGS = 8       # segments per chapter in the two-level pass


def _chapter_budget(n):
    """Two-level chapter pass: every chapter gets its own budget, so total
    cost grows linearly with video length instead of one flat ceiling
    choking on long jobs (v0.3.3 B: 30-minute videos = ~45 segments)."""
    return 2500 + 600 + 600 * n


def _summarize_long(notes, transcript):
    """Two-level summary for long videos (> _LONG_SEGMENTS segments):
    chapter briefs first — each chapter gets an independent budget, and
    the transcript stays out of level 1 (the notes already carry what the
    frames plus dialogue looked like) — then one final pass writes the
    whole-video report from the briefs plus the full transcript, same
    report shape as the single-level pass. An empty chapter brief falls
    back to that chapter's raw notes so no stretch of the video is ever
    silently dropped; only the final report's own truncation marks
    the report truncated."""
    chapters = [notes[i:i + _CHAPTER_SEGS]
                for i in range(0, len(notes), _CHAPTER_SEGS)]
    briefs = []
    for idx, ch in enumerate(chapters, 1):
        sub_msgs = [llm.text_part(
            ui(f"你是视频分析师。下面是全片共{len(chapters)}章里第{idx}章按时间顺序的分段观察记录，",
                   f"You are a video analyst. Below are the time-ordered segment "
                   f"observation notes of chapter {idx} of {len(chapters)}. ")
                + ui("为本章写紧凑小结：\n1) 一句话本章脉络\n"
                     "2) 时间线（时间范围 → 事件），保留原记录里的时间戳，覆盖每段关键内容\n"
                     "只依据记录内容，记录里没写的不许编。\n\n观察记录：\n",
                     "Write a compact chapter brief:\n1) one sentence for the chapter's arc\n"
                     "2) a timeline (time range -> events) keeping the timestamps from the "
                     "notes and covering each segment's key content\n"
                     "Use only what the notes contain; never invent what is not written "
                     "there.\n\nObservation notes:\n")
                + "\n\n".join(ch))]
        sub_budget = _chapter_budget(len(ch))
        content, finish, _u = llm.chat(
            sub_msgs, temperature=0.7, max_tokens=sub_budget,
            reasoning_effort="none")
        content = content or ""
        if finish == "length" and content.strip():
            # 小结也是中间品，同样配续写（11-19 血观音案：话痨模型
            # 预算不够时小结丢尾，最终报告跟着缺一角）。
            sub_msgs = [llm.text_part(
                sub_msgs[0]["text"] + "\n\n"
                + ui("上面的章节小结写到一半断了，请从中断处接完，"
                     "不要重复已写内容：",
                     "The chapter brief above was cut off mid-stream; "
                     "finish it without repeating what is already "
                     "written:")
                + "\n" + content[-600:])]
            more, finish, _u = llm.chat(
                sub_msgs, temperature=0.7,
                max_tokens=min(sub_budget * 2, 16000),
                reasoning_effort="none")
            if (more or "").strip():
                content += "\n" + more.strip()
        briefs.append(content.strip() if content.strip()
                      else "\n\n".join(ch))
    joined = ("\n\n" + ui("—— 章节小结 ——", "—— chapter brief ——")
              + "\n\n").join(
                  ui(f"第{i}章小结：\n", f"chapter {i} brief:\n") + b
                  for i, b in enumerate(briefs, 1))
    audio = (ui("\n\n语音转录时间线(带时间戳，分析对白/旁白时引用)：\n",
                "\n\nSpeech transcript timeline (timestamped; cite it when "
                "analyzing dialogue/narration):\n") + transcript
             if transcript else "")
    final_msgs = [llm.text_part(
            ui("你是视频分析师。下面是一部较长影片按时间顺序的分章小结",
               "You are a video analyst. Below are the chapter-by-chapter "
               "briefs of a longer film, in time order")
            + ui("和语音转录" if transcript else "",
                 " and the speech transcript" if transcript else "")
            + ui("，输出结构化总结：\n1) 一段话概述全片\n2) 分段时间线（时间范围 → 事件，可按章归组）",
                 ", output a structured summary:\n1) one paragraph covering "
                 "the whole film\n2) a timeline (time range -> events, "
                 "grouped by chapter where natural)")
            + ui("\n3) 对白/声音要点与画面如何互相印证" if transcript else "",
                 "\n3) dialogue/audio highlights and how they corroborate "
                 "the visuals" if transcript else "")
            + ui("\n只依据小结与转录内容，里面没写的不许编。\n\n分章小结：\n",
                 "\nUse only what the briefs and transcript contain; never "
                 "invent what is not written there.\n\nChapter briefs:\n")
            + joined + audio)]
    # 最后层的预算按"覆盖全片章节体量"给（每章折 _CHAPTER_SEGS 段，
    # 24 段封顶），不按章数抠——11-19 血观音案：4 段 1 章只分到 4900
    # tokens，喂着 12K 字全量转录，写到 9886 字被铡在最后收束段前。
    budget = _summary_budget([0] * min(24, len(chapters) * _CHAPTER_SEGS),
                             transcript)
    content, finish, _u = llm.chat(
        final_msgs, temperature=0.7, max_tokens=budget,
        reasoning_effort="none")
    content = (content or "").strip()
    if finish == "length" and content:
        # 两级最后层的续写兜底（此前真身是裸的：截尾只打旗不补救）。
        final_msgs = [llm.text_part(
            final_msgs[0]["text"] + "\n\n"
            + ui("上面那份报告写到一半断了，请从中断处接着写完，"
                 "直到完整收尾，不要重复已写内容：",
                 "The report above was cut off mid-stream; continue it "
                 "to a complete finish without repeating what is "
                 "already written:")
            + "\n" + content[-600:])]
        more, finish, _u2 = llm.chat(
            final_msgs, temperature=0.7,
            max_tokens=min(budget * 2, 16000),
            reasoning_effort="none")
        if (more or "").strip():
            content += "\n" + more.strip()
    # 旗只报最终报告自己的截断（11-19 血观音假报案：正文完整收尾，
    # 旗却被章节小结的旧截断累加值拖累误挂）。finish 此时=最后层
    # （含续写）的 finish，正是最终报告的体检结论。
    return content, finish == "length"


def _summarize(notes, transcript=None):
    # v0.3.3 B: long jobs go two-level so the report never dies at a flat
    # ceiling; short jobs keep the single pass (cheaper, same shape).
    if (len(notes) > _LONG_SEGMENTS or
            (transcript and len("\n".join(notes)) + len(transcript)
             > _LONG_INPUT_CHARS)):
        return _summarize_long(notes, transcript)
    body = "\n\n".join(notes)
    audio = (ui("\n\n语音转录时间线(带时间戳，分析对白/旁白时引用)：\n",
                "\n\nSpeech transcript timeline (timestamped; cite it when "
                "analyzing dialogue/narration):\n") + transcript
             if transcript else "")
    msgs = [llm.text_part(
            ui("你是视频分析师。根据下面按时间顺序的分段观察记录",
               "You are a video analyst. Based on the time-ordered segment "
               "observation notes below")
            + ui("和语音转录" if transcript else "",
                 " and the speech transcript" if transcript else "")
            + ui("，输出结构化总结：\n1) 一段话概述全片\n2) 分段时间线（时间范围 → 事件）",
                 ", output a structured summary:\n1) one paragraph covering "
                 "the whole video\n2) a segment timeline "
                 "(time range -> events)")
            + ui("\n3) 对白/声音要点与画面如何互相印证" if transcript else "",
                 "\n3) dialogue/audio highlights and how they corroborate "
                 "the visuals" if transcript else "")
            + ui("\n只依据记录内容，记录里没写的不许编。\n\n观察记录：\n",
                 "\nUse only what the notes contain; never invent what is "
                 "not written there.\n\nObservation notes:\n")
            + body + audio)]
    budget = _summary_budget(notes, transcript)
    content, finish, _u = llm.chat(
        msgs, temperature=0.7, max_tokens=budget,
        reasoning_effort="none")
    content = (content or "").strip()
    if finish == "length" and content:
        # 第三层保险：漏进两级射程之外的单层截断也能续写补全。
        msgs = [llm.text_part(
            msgs[0]["text"] + "\n\n"
            + ui("上面那份报告写到一半断了，请从中断处接着写完，"
                 "直到完整收尾，不要重复已写内容：",
                 "The report above was cut off mid-stream; continue it to a "
                 "complete finish without repeating what is already written:")
            + "\n" + content[-600:])]
        more, finish, _u2 = llm.chat(
            msgs, temperature=0.7, max_tokens=min(budget * 2, 16000),
            reasoning_effort="none")
        if (more or "").strip():
            content += "\n" + more.strip()
    return content, finish == "length"


def _asr_engine(transcribe):
    """Normalize the transcribe knob: legacy True/False plus the v0.1 engine
    names (auto / r2t2 / whisper / cloud / off). Anything unrecognized
    falls back to "auto"."""
    if transcribe is False or (isinstance(transcribe, str)
                               and transcribe.strip().lower() in ("false", "off", "no")):
        return "off"
    if transcribe is True or transcribe in (None, ""):
        return "auto"
    e = str(transcribe).strip().lower()
    return e if e in ("auto", "r2t2", "whisper", "cloud", "off") else "auto"


def _transcribe(path, sub_text, start, end, info, engine="auto",
                on_progress=None, cancel=None):
    """Returns (text, engine_used). Subtitles first: if the user pasted
    SRT/VTT/timed JSON, skip any engine and inject directly ("subtitles").
    Otherwise run the ASR ladder in audio.py with the requested engine
    (auto / r2t2 / whisper / cloud). ``cancel`` is the job's should_stop
    callable: checked before starting and polled by the local workers, so
    a cancelled job force-exits its transcription process at once."""
    if cancel is not None:
        try:
            if cancel():
                return None, None
        except Exception:
            pass
    if sub_text and sub_text.strip():
        on_progress and on_progress(
            ui("注入字幕（未加载 ASR 模型）",
               "injecting subtitles (ASR model not loaded)"))
        return sub_text.strip()[:30000], "subtitles"
    on_progress and on_progress(ui("抽取音轨…", "extracting audio track..."))
    try:
        from . import audio as _audio_mod          # lazy cycle guard
        _ap = _audio_mod.audio_params()            # panel track/channel knobs
        pcm = sampling.audio_pcm16k(path, start, end, probe_info=info,
                                    stream=_ap.get("stream", 0),
                                    channel=_ap.get("channel", 0))
    except Exception:
        # Contract: transcription trouble must never veto finished
        # observations -- degrade to a video-only report instead. (This call
        # used to sit outside the guard below; an ffmpeg hiccup here killed
        # the whole job after the frame legs had all succeeded.)
        on_progress and on_progress(
            ui("音轨抽取失败——跳过语音转录",
               "audio track extraction failed -- skipping speech transcription"))
        return None, None
    if not pcm:
        return None, None
    on_progress and on_progress(
        ui("语音转录中…", f"transcribing speech... (engine: {engine})"))
    try:
        from . import audio
        text, used = audio.transcribe_cues(pcm, engine=engine, cancel=cancel)
        if text:
            on_progress and on_progress(
                ui(f"语音转录完成（引擎 {used}）",
                   f"speech transcription done (engine {used})"))
        return text, used
    except Exception as e:
        # A broken ASR leg should not overturn the whole job: by this point
        # the minutes-long video observation is already done; degrade to
        # "skip speech this run", still emit the report, and make the reason
        # visible in the progress line (11-06). An explicit engine that is
        # not installed surfaces here as its concrete missing-config reason.
        on_progress and on_progress(
            ui(f"语音转录失败，本次跳过语音：{type(e).__name__}: {e}",
               f"speech transcription failed; skipping speech this run: "
               f"{type(e).__name__}: {e}"))
        return None, None


def analyze(path, preset="default", mode=None, fps=None, max_frames=None,
            start=0.0, end=None, strategy="uniform", subtitles=None,
            transcribe="auto", link=True, on_progress=None, should_stop=None,
            resume=None, on_note=None):
    """One full analysis. Returns dict(ok, summary, notes, segments_done,
    total, stopped_early, transcript, transcript_used, transcript_engine,
    elapsed_s). transcribe is the ASR engine knob: "auto" (default, ladder
    over whatever this machine has), "r2t2"/"whisper"/"cloud" (explicit
    engine), "off" (video-only; pasted subtitles still inject). Legacy
    True/False map to auto/off. Videos with no audio track skip
    transcription automatically (there is nothing to transcribe).

    resume={"done":n,"notes":[..]}: skip the first n segments and reuse
    their notes (parameters must match the previous run; the frame plan is
    deterministic); on_note(notes, done, total) fires after every segment.
    """
    t0 = time.time()
    p = resolve_preset(preset)
    mode = mode or p["mode"]
    fps = float(fps or p.get("fps", 8))
    max_frames = int(max_frames or p.get("max_frames", 16))
    if mode == "segment":
        # granularity dropdown = frames PER SEGMENT; the observation request
        # count stays at SEGMENT_TARGET so total cost scales linearly with the
        # chosen granularity (was: hardcoded 64-total / 16-per-seg).
        total_cap = max_frames * SEGMENT_TARGET
    else:
        total_cap = max_frames

    info = sampling.probe(path)
    on_progress and on_progress(
        f"probe: {info['dur']:.1f}s {info['width']}x{info['height']} "
        f"audio={info['has_audio']} {info['size_mib']:.1f}MiB")

    frames, info = sampling.sample(path, start, end, fps=fps,
                                   max_frames=total_cap,
                                   strategy=strategy, probe_info=info)
    on_progress and on_progress(
        ui(f"抽帧完成: {len(frames)} 帧 (strategy={strategy})",
           f"frame sampling done: {len(frames)} frames (strategy={strategy})"))

    if mode == "once":
        segs = [frames[:max_frames]]
    else:
        # segment mode: chunk by the user-chosen granularity (frames/segment)
        segs = _by_size(frames, max_frames)
        if len(segs) > MAX_SEGMENTS:
            segs = segs[:MAX_SEGMENTS]
    seg_total = len(segs)

    done0, notes = 0, []
    if resume:
        done0 = max(0, min(int(resume.get("done", 0)), seg_total))
        notes = list(resume.get("notes", []))[:done0]
        on_progress and on_progress(
            ui(f"断点续跑：复用 {done0}/{seg_total} 段",
               f"resuming: reusing {done0}/{seg_total} segments"))

    stopped = False
    for i in range(done0, seg_total):
        if should_stop and should_stop():
            stopped = True
            break
        on_progress and on_progress(
            ui(f"观察分段 {i + 1}/{seg_total} ({len(segs[i])} 帧)",
               f"observing segment {i + 1}/{seg_total} ({len(segs[i])} frames)"))
        note = _observe(segs[i], i + 1, seg_total,
                        notes[-1] if (link and notes) else "",
                        f"{_ts(segs[i][0][0])}-{_ts(segs[i][-1][0])}")
        notes.append(note)
        on_note and on_note(notes, i + 1, seg_total)

    transcript = None
    transcript_engine = None
    engine = _asr_engine(transcribe)
    if (engine != "off" and info["has_audio"]) or subtitles:
        if not stopped or notes:
            transcript, transcript_engine = _transcribe(
                path, subtitles, start,
                end if end is not None else info["dur"],
                info, engine=engine, on_progress=on_progress,
                cancel=should_stop)

    summary_truncated = False
    if notes and not stopped:
        on_progress and on_progress(
            ui("notes 汇总中…", "compressing notes..."))
        notes = _compress(notes, on_progress)
        on_progress and on_progress(ui("生成最终总结…", "generating final summary..."))
        summary, summary_truncated = _summarize(notes, transcript)
    elif notes:
        summary = ui(
            "（分析已取消，未生成全片总结；"
            "已完成的分段记录见「逐段观察记录」区）",
            "(analysis cancelled; no whole-video summary was "
            "generated — completed segment notes are in the "
            "\"Per-segment observation notes\" section)")
    else:
        summary = ui("（未产生任何分段记录）", "(no segment notes were produced)")

    return {"ok": not stopped and bool(notes),
            "summary": summary, "notes": notes,
            "segments_done": len(notes), "total": seg_total,
            "stopped_early": stopped, "transcript_used": bool(transcript),
            "transcript": transcript, "summary_truncated": summary_truncated,
            "transcript_engine": transcript_engine,
            "video": path, "probe": info,
            "elapsed_s": round(time.time() - t0, 1)}
