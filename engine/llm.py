"""Analysis engine client: one single path -- Hermes' official auxiliary LLM
router (agent.auxiliary_client.call_llm, aux task "video_xray").

The router resolves provider, model and credentials from Hermes' own config
and live runtime, so the plugin works out of the box on a local llama.cpp /
Strata engine AND on every cloud provider Hermes supports (OpenAI,
Anthropic, OpenRouter, custom ...) -- no plugin-specific auth, base-URL or
port code at all. The plugin registers the "video_xray" auxiliary task at
load time; a user who wants a dedicated analysis model just sets
auxiliary.video_xray.provider/.model in config.yaml (standard auxiliary
config, also editable from the desktop command center).

Verified pitfall (local thinking engines, 11-06): reasoning arrives in
reasoning_content and silently eats max_tokens -- pure text-shuffling calls
(summary/compression) pass reasoning_effort="none", the only key this
engine family honors. Strict cloud APIs may reject that borrowed key, so
chat() retries once with extra_body dropped: correctness beats the speed
win there.

Display language: system_language() reads display.language from config.yaml
-- the exact key the desktop UI uses -- so reports follow the app language;
ui(zh, en) picks one. config_get() is a small stdlib YAML line-scan for the
plugin's own top-level `video-xray:` section (used for ASR tuning only;
the LLM side needs no plugin config at all).
"""
import base64
import os
import time

TASK = "video_xray"          # aux task key registered in the plugin's register()

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


def _content_of(resp):
    """Prefer the router's own extractor (content, else reasoning fallback);
    fall back to a plain OpenAI-shape read for odd return types."""
    try:
        from agent.auxiliary_client import extract_content_or_reasoning
        text = extract_content_or_reasoning(resp)
        if text:
            return text
    except Exception:  # noqa: BLE001 -- extractor optional, host shapes vary
        pass
    try:
        ch = resp["choices"][0] if isinstance(resp, dict) else resp.choices[0]
        msg = ch["message"] if isinstance(ch, dict) else ch.message
        if isinstance(msg, dict):
            return msg.get("content") or msg.get("reasoning_content") or ""
        return getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or ""
    except Exception:  # noqa: BLE001
        return ""


def _finish_of(resp):
    try:
        ch = resp["choices"][0] if isinstance(resp, dict) else resp.choices[0]
        return (ch.get("finish_reason") if isinstance(ch, dict)
                else getattr(ch, "finish_reason", None)) or ""
    except Exception:  # noqa: BLE001
        return ""


def _usage_of(resp):
    try:
        u = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
        if u is None:
            return {}
        if isinstance(u, dict):
            return u
        dump = getattr(u, "model_dump", None)
        return dump() if callable(dump) else {}
    except Exception:  # noqa: BLE001
        return {}


# The desktop panel swaps the chat model at runtime (model selector) but
# does NOT rewrite config.yaml's model section -- only the gateway's
# in-process runtime mirror (refreshed every turn, readable from any
# thread) holds the truth. The official router falls back to config.yaml
# on worker threads (_compat_runtime_main() is main-thread-only), so
# analysis used to die on a stale, already-dead port while chat ran fine
# (2026-11-09 incident: chat live on 127.0.0.1:8080 SC117, config still
# said dead 47831 SWIFT -> every job APIConnectionError, engine looked
# "stuck"). Reading the mirror here and handing it to
# call_llm(main_runtime=...) pins every analysis request to the very
# model the user is chatting with -- instantly, one path, no config lag.
# main_runtime is the official call_llm hook and never writes gateway
# state back.
def live_main_runtime():
    """The session's live model as a call_llm ``main_runtime`` dict.

    Returns {} when the mirror is empty (e.g. CLI without runtime binding,
    -> plain config fallback as before) or when the user pinned an
    explicit route for this task (config auxiliary.video_xray model /
    base_url -- that route wins, so pass nothing and the router uses it).
    """
    try:
        aux = config_get("auxiliary", TASK) or {}
    except Exception:
        aux = {}
    if isinstance(aux, dict) and (aux.get("model") or aux.get("base_url")):
        return {}
    try:
        from agent import auxiliary_client as _ac
        fields = tuple(getattr(_ac, "_MAIN_RUNTIME_FIELDS", ()) or ())
        snap = tuple(getattr(_ac, "_RUNTIME_MAIN_COMPAT_SNAPSHOT", ()) or ())
    except Exception:
        return {}
    if not fields or len(fields) != len(snap):
        return {}
    rt = {k: v for k, v in zip(fields, snap) if v}
    if not rt.get("model") or not rt.get("base_url"):
        return {}
    return rt


def chat(parts, temperature=0.0, max_tokens=1600, timeout=300, reasoning_effort=None):
    """Send one chat turn through the official router. Returns
    (content, finish_reason, usage).

    The same OpenAI-style content parts (text + data-URL image_url) go in as
    the single user message; call_llm's adapter translates them per provider
    (Anthropic image blocks etc.) -- which is exactly why the plugin needs
    no provider code of its own.
    """
    try:
        from agent.auxiliary_client import call_llm
    except ImportError as e:
        raise RuntimeError(ui(
            "宿主 Hermes 没有官方辅助调用接口"
            "（agent.auxiliary_client），请升级 Hermes 后重试",
            "the host Hermes has no official auxiliary client (agent.auxiliary_client); "
            "upgrade Hermes and retry")) from e
    kwargs = dict(task=TASK, messages=[{"role": "user", "content": parts}],
                  temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    rt = live_main_runtime()
    if rt:
        kwargs["main_runtime"] = rt
    if reasoning_effort:
        kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
    err = None
    resp = None
    while True:
        try:
            resp = call_llm(**kwargs)
            err = None
            break
        except Exception as e:  # noqa: BLE001 -- router raises provider-specific errors
            err = e
            # A strict cloud endpoint (or an older host without extra_body)
            # may choke on the borrowed thinking-control key: drop it and
            # give the call one clean retry.
            if kwargs.pop("extra_body", None) is not None:
                continue
            break
    if err is not None:
        brief = f"{type(err).__name__}: {err}"[:300]
        raise RuntimeError(ui(f"语言模型调用失败：{brief}",
                              f"LLM call failed: {brief}"))
    if resp is None:
        raise RuntimeError(ui(
            "Hermes 还没有可用的语言模型"
            "（本插件跟随会话模型；请先在 Hermes 配好模型再重试视频）",
            "Hermes has no usable language model yet (this plugin follows your chat "
            "model; configure one in Hermes, then retry the video)"))
    return _content_of(resp), _finish_of(resp), _usage_of(resp)
