"""Analysis engine client: one single path -- the host's official plugin LLM
API (ctx.llm.complete), with task slots this plugin registers at load time.

The host resolves provider, model, credentials and routing from the user's
own Hermes setup, so the plugin works out of the box on a local llama.cpp /
Strata engine AND on every cloud provider Hermes supports (OpenAI,
Anthropic, OpenRouter, custom ...) -- zero plugin-specific auth, base-URL
or port code. It also means analysis always rides the exact chat model the
user is using THIS second: the host routes live, so no stale config port
can ever strand an analysis (the 2026-11-10 dead-47831 incident this design
retires -- its main_runtime mirror hack and its hand-rolled extra_body
retry ladder both become host-side concerns: the host itself retries a
provider 400 that rejects a reasoning wire control by name, #112781).

Two task slots split the reasoning budget:
- OBSERVE_TASK "video_xray"           -> frame observation and H3 prompts,
  thinking left at the model/provider default (vision observation benefits).
- SUMMARY_TASK "video_xray_summary"  -> report writing and note compression;
  registered with defaults {"reasoning_effort": "none"}: text-shuffling
  calls must not let thinking silently eat max_tokens and clip the report
  (local thinking engines put reasoning in reasoning_content; the only key
  this engine family honors is a top-level reasoning_effort). Operators can
  override either slot via the standard auxiliary.<key> config block.

The host result carries no finish_reason, so output-token usage (zero when
a provider reports none) approximates the old finish=="length" truncation
signal; callers keep the exact (content, finish, usage) contract unchanged
-- including the old failure mode: chat() raises a localized RuntimeError
so jobs surface one clean reason instead of dying inside the gateway.

Display language: system_language() reads display.language from config.yaml
-- the exact key the desktop UI uses -- so reports follow the app language;
ui(zh, en) picks one. config_get() is a small stdlib YAML line-scan for the
plugin's own top-level `video-xray:` section (used for ASR tuning only;
the LLM side needs no plugin config at all).
"""
import base64
import os
import time

OBSERVE_TASK = "video_xray"           # aux task slots registered in register()
SUMMARY_TASK = "video_xray_summary"

_CTX = {"ctx": None}


def set_ctx(ctx):
    """Called once by the plugin's register(ctx); every LLM call from then
    on goes through ctx.llm -- the host's official, fail-closed path."""
    _CTX["ctx"] = ctx


def llm_ready():
    return getattr(_CTX["ctx"], "llm", None) is not None


_HERMES_CFG = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "config.yaml")

_lang_cache = {"lang": None, "t": 0.0}
_cfg_cache = {"lines": None, "t": 0.0}


def _cfg_lines():
    """config.yaml lines, 60 s cache (the file is tiny; a line-scan beats
    pulling a YAML lib into the engine)."""
    now = time.time()
    if _cfg_cache["lines"] is not None and now - _cfg_cache["t"] < 60:
        return _cfg_cache["lines"]
    try:
        with open(_HERMES_CFG, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    _cfg_cache["lines"] = lines
    _cfg_cache["t"] = now
    return lines


def config_get(*keys):
    """Scalar string at a nested path of config.yaml, e.g.
    config_get("video-xray", "asr", "device"). Stdlib indent-stack scan;
    list items and multi-line blocks are ignored. None when absent."""
    want = [str(k) for k in keys]
    stack = []                       # (indent, key)
    for ln in _cfg_lines():
        if not ln.strip() or ln.lstrip().startswith("#") or ":" not in ln:
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        raw_key, _, raw_val = ln.strip().partition(":")
        key = raw_key.strip()
        if not key or key.startswith("-"):   # skip list items
            continue
        val = raw_val.split(" #")[0].strip().strip('"').strip("'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if [k for _, k in stack] == want and val:
            return val
    return None


def system_language():
    """App display language: 'zh' | 'en' (default 'zh').

    Source of truth: display.language in config.yaml -- the very key the
    desktop UI reads -- so reports follow whatever language the user set,
    regardless of which model or provider is loaded. VA_LANG=zh|en in the
    environment overrides (for tests). Cached 60 s.
    """
    now = time.time()
    if _lang_cache["lang"] is not None and now - _lang_cache["t"] < 60:
        return _lang_cache["lang"]
    env = os.environ.get("VA_LANG", "").strip().lower()
    if env:
        lang = "en" if env.startswith("en") else "zh"
    else:
        val = (config_get("display", "language") or "zh").lower()
        lang = "en" if val.startswith("en") else "zh"
    _lang_cache["lang"] = lang
    _lang_cache["t"] = now
    return lang


def ui(zh, en):
    """Pick the user-facing string for the current display language."""
    return en if system_language() == "en" else zh


def image_part(jpg_bytes):
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg_bytes).decode()}}


def text_part(s):
    return {"type": "text", "text": s}


def chat(parts, temperature=0.0, max_tokens=1600, timeout=300, reasoning_effort=None):
    """Send one chat turn through the host plugin LLM. Returns
    (content, finish, usage).

    The same OpenAI-style content parts (text + data-URL image_url) go in
    as the single user message; the host's adapters translate them per
    provider (Anthropic image blocks etc.) -- which is exactly why the
    plugin carries no provider code of its own.

    reasoning_effort="none" routes to the summary slot whose registered
    defaults pin thinking off; anything else uses the observation slot.
    finish is "stop" unless the host reports output tokens at (or within
    8 of) max_tokens -- the provider-side cutoff signal, approximated
    because the host result has no finish_reason. Raises a localized
    RuntimeError when the call fails, same contract as before.
    """
    llm = getattr(_CTX["ctx"], "llm", None)
    if llm is None:
        raise RuntimeError(ui(
            "宿主 LLM 入口未接线（ctx.llm 缺失）——请升级 Hermes 或重启网关后重试",
            "the host LLM entry point is not wired (ctx.llm missing) — "
            "upgrade Hermes or restart the gateway, then retry"))
    task = SUMMARY_TASK if reasoning_effort == "none" else OBSERVE_TASK
    messages = [{"role": "user", "content": list(parts)}]
    try:
        res = llm.complete(messages, temperature=temperature,
                           max_tokens=max_tokens, timeout=timeout, task=task)
    except Exception as err:  # noqa: BLE001 -- host raises provider errors; surface one line
        brief = f"{type(err).__name__}: {err}"[:300]
        raise RuntimeError(ui(f"语言模型调用失败：{brief}",
                              f"LLM call failed: {brief}")) from err
    text = getattr(res, "text", None)
    if text is None:
        text = str(res or "")
    usage_obj = getattr(res, "usage", None)
    out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
    finish = "length" if out_tok and out_tok + 8 >= max_tokens else "stop"
    return text, finish, {
        "output_tokens": out_tok,
        "model": str(getattr(res, "model", "") or ""),
        "provider": str(getattr(res, "provider", "") or ""),
    }


def probe():
    """Gate check: one tiny real call through the same slot the jobs use,
    so probe green == job green. Reports the model and provider the host
    actually routed to; on failure carries the host's own one-line reason
    (e.g. connection refused against the currently selected engine)."""
    if not llm_ready():
        # One-shot field diagnostic: tells apart the ways wiring can fail
        # (register never ran in THIS process / ctx without .llm).
        _ctx = _CTX["ctx"]
        try:
            from agent.auxiliary_task_config import registered_tasks
            _aux = "aux-ok" if "video_xray" in set(registered_tasks()) else "aux-missing"
        except Exception as _e:
            _aux = "aux-err:" + type(_e).__name__
        _diag = (f" [diag pid={os.getpid()} ctx="
                 f"{'none' if _ctx is None else 'no-llm-attr'} {_aux}]")
        return {"ok": False, "model": "?", "provider": "?", "error": ui(
            "宿主 LLM 入口未接线（ctx.llm 缺失）——请升级 Hermes 或重启网关后重试",
            "the host LLM entry point is not wired (ctx.llm missing) — "
            "upgrade Hermes or restart the gateway, then retry") + _diag}
    try:
        _t, _f, usage = chat([text_part("Reply with the single word OK.")],
                             temperature=0.0, max_tokens=8, timeout=15.0)
    except RuntimeError as err:
        return {"ok": False, "model": "?", "provider": "?",
                "error": str(err)[:300]}
    return {"ok": True,
            "model": usage.get("model") or "?",
            "provider": usage.get("provider") or "?",
            "error": ""}
