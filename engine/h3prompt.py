"""Reverse-build the analysis ledger into a MiniMax-H3 shot prompt + an
exportable Markdown report.

The format is not invented -- it was scraped verbatim from the master's
finished-video PNG metadata prompt (11-06):

  Format A (single shot, feeds MiniMaxH3AudioConditioningT8.prompt):
    one continuous Chinese block, order = scene mood -> camera language ->
    audio keynote -> character look -> continuous action (dialogue quoted
    verbatim in "" marks) -> camera move -> stability sentence -> audio/
    picture sync sentence

  Format B (shot-split, feeds TE_H3_Prompt_Enhancer / TE_text_display):
    integrated_multimodal_description:
    [Shot 1] ...

    [Shot 2] At 00:03.000, ...

    overall_soundscape: ...

    non_diegetic_music: N/A
    -- note: [Shot 1] carries no timestamp; only N>=2 gets "At MM:SS.000, ".

Shot boundaries come from ffmpeg's real scene cuts (sampling._scene_times),
not from the engine's analysis slices: measured on the master's 10 s
one-take clip, thresholds 0.15-0.6 all yield 0 cuts (= 1 shot), while a
20 s sample yields 3 cuts at 0.3 (= 4 shots); the probe runs in 0.2 s.

Two assembly paths: rule (verbatim bucket-join of the observation notes,
zero hallucination, instant) / llm (the engine rewrites them into flowing
prose, thinking disabled, slower but smoother).

The H3 payload itself (system prompts, shot glue, soundscape lines) stays
canonical Chinese regardless of UI language -- that is the exact format the
H3 nodes are trained to consume. Only human-facing output (the Markdown
report) follows the display language via llm.ui().
"""
import datetime
import json
import re

from . import llm, sampling

ui = llm.ui

# The stability + sync sentences from the master's original prompt (gender-
# neutral; these are the two lines H3 recognizes -- canonical, never i18n'd).
_STABLE = "保留角色的身份特征、自然的身体结构、稳定的四肢、以及流畅连贯的动作。"
_SYNC = "角色的说话声需与口型同步，环境音需与画面内容同步。"

_EV_RE = re.compile(r"\[(\d{1,2}:\d{2})(?::(\d{2}))?(?:\s*-\s*(\d{1,2}:\d{2})(?::(\d{2}))?)?\]")
_CUE_RE = re.compile(r"^(\d{1,2}:\d{2})(?::(\d{2}))?\s+(.+)$", re.M)


def _full(head, tail):
    """The regex splits H:MM:SS into two groups; glue them back before
    converting, so the hour never gets dropped."""
    return head + (":" + tail if tail else "")


def _secs(s):
    """'MM:SS' / 'MM:SS.ss' / 'H:MM:SS' -> seconds (the ledger stores whole
    strings; do not split by capture group)."""
    if not s:
        return 0.0
    parts = s.split(":")
    try:
        v = 0.0
        for p in parts:
            v = v * 60 + float(p)
        return v
    except ValueError:
        return 0.0


# Analytical meta-commentary that sometimes rides along in the observation
# notes; H3 does not want it, so the verbatim assembly drops those lines.
# Both language variants of the truncation marker are listed (notes follow
# the display language, 11-08).
_META = ("首尾对比", "并非静止镜头", "画面变化显著", "变化明显", "内容被截断",
         "Content truncated")


def _clean(txt):
    """Raw notes -> prompt-usable prose: strip bullets/newlines, drop meta
    commentary."""
    out = []
    for line in (txt or "").splitlines():
        line = line.strip().lstrip("-*\u2010\u2011\u2012\u2013 ").strip()
        if not line or any(k in line for k in _META):
            continue
        out.append(line)
    return " ".join(out)


def events_from_notes(notes):
    """Observation notes -> [(t0, t1, text)].

    Each engine note segment is one long string embedding [00:00-00:01] /
    [00:01] / [01:23] timestamps; cut it into events at those timestamps.
    Leading lines without a timestamp (the segment headers) are dropped.
    """
    evs = []
    for seg in notes or []:
        for m in _EV_RE.finditer(seg or ""):
            t0 = _secs(_full(m.group(1), m.group(2)))
            t1 = _secs(_full(m.group(3), m.group(4))) if m.group(3) else t0 + 1.0
            if t1 <= t0:
                t1 = t0 + 1.0
            nxt = _EV_RE.search(seg, m.end())
            txt = _clean(seg[m.end():nxt.start() if nxt else len(seg)])
            if txt:
                evs.append((t0, t1, txt))
    evs.sort(key=lambda e: e[0])
    return evs


def cues_from_transcript(tr):
    """Transcript lines '00:02 dialogue' -> [(2.0, 'dialogue')]."""
    out = []
    for m in _CUE_RE.finditer(tr or ""):
        txt = m.group(3).strip()
        if txt:
            out.append((_secs(_full(m.group(1), m.group(2))), txt))
    out.sort(key=lambda c: c[0])
    return out


# Valid threshold range: below 0.05 even noise counts as a cut, above 0.95
# almost nothing cuts.
THRESH_MIN, THRESH_MAX = 0.05, 0.95
# Default threshold: aligned with the green-bar dropdown's "safe 0.3" --
# single source of truth.
DEFAULT_THRESHOLD = 0.30


def shot_bounds(path, dur, thresh=DEFAULT_THRESHOLD):
    """Real shot boundaries (cut list; empty = one continuous take). If the
    video cannot be probed, fall back to an empty list."""
    try:
        return list(sampling._scene_times(path, 0.0, dur, thresh=thresh))
    except Exception:
        return []


def bucket(evs, bounds, dur):
    """Bucket events by shot cuts; bounds=[] -> everything in one bucket."""
    edges = [0.0] + [b for b in bounds if 0.0 < b < dur] + [max(dur, 0.1) + 0.001]
    buckets = [[] for _ in range(len(edges) - 1)]
    for ev in evs:
        i = 0
        for k in range(len(edges) - 1):
            if ev[0] >= edges[k] - 0.001:
                i = k
        buckets[min(i, len(buckets) - 1)].append(ev)
    # Empty buckets are dropped, but the window each kept bucket is assembled
    # for must travel with it — keeping every edge would shift later shots'
    # time windows left (wrong [Shot N] stamps, cues in the wrong shot).
    live = [i for i, b in enumerate(buckets) if b]
    return [buckets[i] for i in live], [(edges[i], edges[i + 1]) for i in live]


def _fmt_at(t):
    """The master's original prompt uses At 00:03.000 (MM:SS.mmm); copy that
    style verbatim."""
    m, s = int(t // 60), t % 60
    return "%02d:%06.3f" % (m, s)


def _end_p(body):
    """Close the segment with sentence-ending punctuation so the glue never
    leaves two clauses jammed together."""
    if body and body[-1] not in "。！？；.!?":
        body += "。"
    return body


def _join(items):
    """Observation entries already end with punctuation; join them directly,
    adding no stray punctuation or spaces."""
    out = []
    for t in items:
        t = t.strip()
        if not t:
            continue
        if out and out[-1][-1] not in "。！？；.!?":
            out[-1] = out[-1] + "。"
        out.append(t)
    return "".join(out)


def _cues_in(cues, lo, hi):
    return [c for c in cues if lo - 0.001 <= c[0] < hi]


def _sound_lines(cues, evs):
    """Ambience/music can only be half-inferred: the visual observer never
    hears, and ASR only yields human speech."""
    human = ("清晰的近距离人声台词共%d句（见对应镜头段的引号内容）" % len(cues)) if cues \
        else "无人声台词（转录未拾到语音）"
    hint = ""
    for _, _, txt in evs[:2]:
        for kw in ("街道", "马路", "车内", "办公室", "厨房", "客厅", "卧室", "玄关",
                   "雨", "雪", "风", "人群", "商场", "教室", "舞台", "海边", "森林"):
            if kw in txt:
                hint = "，环境音按画面场景推测应含%s相关声响" % kw
                break
        if hint:
            break
    return ("overall_soundscape: %s%s（注：环境音由画面推测，非听音分析）" % (human, hint),
            "non_diegetic_music: N/A")


def assemble_rule(shot_evs, edges, cues, mode):
    """Pure rule assembly: observation prose enters the shots verbatim, zero
    hallucination. Glue words stay canonical Chinese (H3 payload format)."""
    shots = []
    for i, evs in enumerate(shot_evs):
        lo, hi = edges[i]
        body = _join([e[2] for e in evs])
        for _, txt in _cues_in(cues, lo, hi):
            body = _end_p(body) + '画内人说：“%s”。' % txt
        shots.append((lo, body))
    sound, music = _sound_lines(cues, [e for evs in shot_evs for e in evs])
    if mode == "single":
        head = shots[0][1] if shots else ""
        for lo, body in shots[1:]:
            head = _end_p(head) + "随后，" + body
        return "\n".join([head, _STABLE, _SYNC])
    segs = []
    for i, (lo, body) in enumerate(shots):
        if i == 0:
            segs.append("[Shot 1] " + body)
        else:
            segs.append("[Shot %d] At %s, %s" % (i + 1, _fmt_at(lo), body))
    return ("integrated_multimodal_description:\n" + "\n\n".join(segs)
            + "\n\n" + sound + "\n\n" + music)


_SYS = """你是视频生成模型的提示词整理器。把给定的逐帧观察记录和台词，整理成 MiniMax-H3 认的分段提示词。

硬约束（违反即废）：
1. 只能用给定事实，禁止新增人物、道具、地点、情绪、风格词；不确定的不写。
2. 台词一字不改，原样放进中文引号“”里。
3. 每个 [Shot N] 一段话，写清该镜头的场景与光影、构图景别、角色外观、动作细节、镜头运动，段内按时间顺序连贯。
4. [Shot 1] 不带时间戳；[Shot 2] 起必须以 "At MM:SS.000, " 开头（分、秒.毫秒）。
5. 全部段落之后紧跟两行：overall_soundscape: ……  和  non_diegetic_music: N/A。
6. 忽略"首尾对比""并非静止镜头"这类分析性元话语，只写画面事实。
7. 只输出正文，不要解释、不要代码围栏。

格式范例：
integrated_multimodal_description:
[Shot 1] 写实风格，夜晚城市街道霓虹，雨后路面反光。镜头从中景缓慢推进，一名穿米色风衣的短发女子撑伞走在人行道，背景车辆灯光虚化。

[Shot 2] At 00:03.000, 承接上一镜头，女子停下脚步转头看向画面右侧，伞沿滴水，她说：“你终于来了。”，镜头继续推近至面部近景。

overall_soundscape: 湿滑路面脚步声与远处车流声，雨点打在伞面上的细碎声响，清晰的近距离人声台词。

non_diegetic_music: N/A
"""

_SYS_SINGLE = """你是视频生成模型的提示词整理器。把给定的逐帧观察记录和台词，整理成 MiniMax-H3 认的单镜头连续提示词（一整段连续中文，不用 [Shot N] 标记）。

硬约束（违反即废）：
1. 只能用给定事实，禁止新增人物、道具、地点、情绪、风格词。
2. 台词一字不改，原样放进中文引号“”里。
3. 顺序：场景氛围 → 镜头语言（写清是一镜到底还是推拉/平移）→ 角色外观 → 按时间顺序的连续动作与台词 → 镜头收尾运动。
4. 结尾原样补上这两句：保留角色的身份特征、自然的身体结构、稳定的四肢、以及流畅连贯的动作。角色的说话声需与口型同步，环境音需与画面内容同步。
5. 忽略"首尾对比""并非静止镜头"这类分析性元话语，只写画面事实。
6. 只输出正文，不要解释、不要代码围栏。

格式范例：
夜晚，下着小雨的霓虹街道上，一个连续的电影感长镜头，配一段适合这种场景的背景音乐。一位穿米色风衣的短发女子撑伞走在人行道，她停下脚步转头看向画面右侧，伞沿滴水，她说：“你终于来了。”，镜头继续推近至她的面部近景。保留角色的身份特征、自然的身体结构、稳定的四肢、以及流畅连贯的动作。角色的说话声需与口型同步，环境音需与画面内容同步。
"""


def _inputs_block(shot_evs, edges, cues):
    lines = ["【逐帧观察（按真实镜头分好桶，时间戳单位秒）】"]
    for i, evs in enumerate(shot_evs):
        lo, hi = edges[i]
        lines.append("镜头 %d（%.2f-%.2f 秒）:" % (i + 1, lo, hi))
        for t0, t1, txt in evs:
            lines.append("  [%.2f-%.2f] %s" % (t0, t1, txt))
    lines.append("")
    lines.append("【台词（带时间戳，必须原样引用）】")
    for t, txt in cues or []:
        lines.append("  [%.2f] %s" % (t, txt))
    return "\n".join(lines)


def assemble_llm(shot_evs, edges, cues, mode, max_tokens=1400):
    """Engine rewrites the notes into flowing prose (thinking off). On
    validation failure / truncation return None so the caller falls back to
    the rule assembly."""
    sys_txt = _SYS if mode != "single" else _SYS_SINGLE
    parts = [llm.text_part(sys_txt + "\n\n" + _inputs_block(shot_evs, edges, cues))]
    try:
        # llm.chat() has no `retries` parameter — passing one raised TypeError
        # on every call, silently downgrading ALL LLM polish to the rule path
        # while info["engine"] still claimed "llm". Keep to the real signature.
        content, finish, _ = llm.chat(parts, temperature=0.2, max_tokens=max_tokens,
                                      timeout=180, reasoning_effort="none")
    except Exception:
        return None
    txt = (content or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("text"):
            txt = txt[4:]
        txt = txt.strip()
    if not txt or finish == "length":
        return None
    if mode == "single":
        if "[Shot" in txt or len(txt) < 120:
            return None
    else:
        if "integrated_multimodal_description:" not in txt or "[Shot 1]" not in txt:
            return None
    return txt


def markdown(job, h3, info):
    """Human-readable Markdown report (report / dialogue / observation notes
    / H3 prompt appendix). Headings follow the display language; the H3
    payload block itself stays canonical Chinese."""
    pr = job.get("params") or {}
    p = sampling.probe(pr.get("path") or "") if pr.get("path") else {}
    name = (pr.get("path") or job.get("id", "")).replace("\\", "/").split("/")[-1]
    L = [ui("# 视频分析：%s", "# Video analysis: %s") % name, ""]
    meta = [
        ui("- 时长 %.1f 秒 | %sx%s", "- Duration %.1f s | %sx%s")
        % (p.get("dur", 0), p.get("width", "?"), p.get("height", "?")),
        ui("- 档位 %s | 每段帧数上限 %s | 语音转录 %s",
           "- Preset %s | max frames per segment %s | speech transcript %s")
        % (pr.get("preset", "?"), pr.get("max_frames", "?"),
           ui("已用", "used") if job.get("transcript_used") else ui("未用/无", "none")),
        ui("- 真实镜头数 %d（阈值 %s） | H3 模式 %s | 组装 %s",
           "- Real shots %d (threshold %s) | H3 mode %s | assembled by %s")
        % (info.get("shots", 0), info.get("thresh", "?"),
           ui("单镜", "single-shot") if info.get("mode") == "single"
           else ui("分段", "shot-split"), info.get("engine", "?")),
        ui("- 分析耗时 %s 秒 | 导出 %s", "- Analysis took %s s | exported %s")
        % (job.get("elapsed_s", "?"),
           datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))]
    if info.get("warning"):
        meta.append(ui("- 注意：%s", "- Note: %s") % info["warning"])
    L += meta + ["", ui("## 报告", "## Report"),
                 (job.get("summary") or ui("（无报告）", "(no report)")).strip(), ""]
    if job.get("transcript"):
        L += [ui("## 台词（语音转录）", "## Dialogue (speech transcript)"),
              "```text", job["transcript"].strip(), "```", ""]
    notes = job.get("notes") or []
    if notes:
        L += [ui("## 逐段观察记录", "## Per-segment observation notes")]
        for i, s in enumerate(notes, 1):
            L += ["", ui("### 段 %d", "### Segment %d") % i, (s or "").strip(), ""]
    L += [ui("## H3 提示词（可直接粘进 ComfyUI 节点）",
             "## H3 prompt (paste straight into the ComfyUI node)"),
          "```text", h3.strip(), "```", ""]
    return "\n".join(L)


def build(job, mode="shots", thresh=DEFAULT_THRESHOLD, use_llm=False):
    """Ledger -> {'h3','md','shots','mode','thresh','engine','warning'}."""
    pr = job.get("params") or {}
    path = pr.get("path") or ""
    try:
        dur = float(sampling.probe(path).get("dur") or 0)
    except Exception:
        dur = 0.0
    evs = events_from_notes(job.get("notes"))
    cues = cues_from_transcript(job.get("transcript"))
    bounds = shot_bounds(path, dur, thresh) if path and dur else []
    shot_evs, edges = bucket(evs, bounds, dur)
    if not shot_evs:
        return {"h3": "", "md": "", "shots": 0, "mode": mode, "thresh": thresh,
                "engine": "none",
                "warning": ui("账本里没有逐帧观察记录，反推不出提示词",
                              "this ledger has no frame observation notes; "
                              "nothing to build a prompt from")}
    warning = ""
    h3 = engine = None
    if use_llm:
        budget = max(900, 320 * len(shot_evs) + 300)
        h3 = assemble_llm(shot_evs, edges, cues, mode, max_tokens=budget)
        engine = "llm"
        if h3 is None:
            warning = ui("模型整理未通过校验（或被切断），已退回逐字拼接版",
                         "the LLM assembly failed validation (or was cut off); "
                         "fell back to the verbatim rule assembly")
    if h3 is None:
        h3 = assemble_rule(shot_evs, edges, cues, mode)
        # Text on disk is the rule assembly — claim "rule", not the "llm"
        # we merely attempted. (Was `engine or "rule"`, which kept the lie.)
        engine = "rule"
    info = {"shots": len(shot_evs), "mode": mode, "thresh": thresh,
            "engine": engine, "warning": warning}
    return dict(h3=h3, md=markdown(job, h3, info), **info)
