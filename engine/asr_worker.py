"""Short-lived ASR worker: WAV bytes on stdin -> faster-whisper -> one line
of JSON cues on stdout.

Runs under the va-asr venv's python (that is where faster-whisper lives);
process exit releases the VRAM right away.

Env knobs (injected by the panel settings via video-xray audio.py):
  VA_ASR_MODEL       explicit model dir (absolute snapshot path; top priority)
  VA_ASR_MODEL_SIZE  tiny|base|small|medium|large-v3 (default small) —
                     resolved against the local HF hub cache first; when the
                     size is not cached we fall back to the repo name so an
                     online user just works (offline users must pre-download)
  VA_ASR_DEVICE      auto|cpu|cuda (default auto: cuda float16 first, fall
                     back to cpu int8 — tried once each, no ping-ponging)
  VA_ASR_LANG        language name; empty/auto = auto-detect
  VA_ASR_PROMPT      initial_prompt (faster-whisper has no true hotword
                     API; the prompt biases proper nouns approximately)
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

# Self-defense: when the gateway spawns us it leaks PYTHONPATH (the
# harness's site-packages); its faster_whisper + PyAV19 would shadow this
# venv's own fw + av16 and blow up on av.open(metadata_errors=) TypeError.
# Filter the polluted entries out of sys.path.
_pp = {os.path.normcase(p) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p}
if _pp:
    sys.path[:] = [p for p in sys.path if os.path.normcase(p) not in _pp]

SIZES = ("tiny", "base", "small", "medium", "large-v3")


def _env(key, default=""):
    v = (os.environ.get(key) or "").strip()
    return v or default


def model_src():
    """Explicit path > local HF snapshot for the requested size > repo name
    (online download fallback)."""
    src = _env("VA_ASR_MODEL")
    if src:
        return src
    size = _env("VA_ASR_MODEL_SIZE", "small").lower()
    if size not in SIZES:
        size = "small"
    snap = (Path.home() / ".cache" / "huggingface" / "hub"
            / f"models--Systran--faster-whisper-{size}" / "snapshots")
    if snap.is_dir():
        for d in sorted(snap.iterdir()):
            if (d / "model.bin").is_file():
                return str(d)
    return size          # not cached: faster-whisper fetches by repo name


def load():
    from faster_whisper import WhisperModel
    src = model_src()
    # HF_HUB_OFFLINE was defaulted to "1" so a cached snapshot never
    # stalls on the network; but a size that is NOT cached can only be
    # fetched online — clear the flag for that path or a fresh user
    # could never get a model at all.
    if not (os.path.isabs(src) or os.path.exists(src)):
        os.environ["HF_HUB_OFFLINE"] = "0"
    device = _env("VA_ASR_DEVICE", "auto").lower()
    if device == "cpu":
        return WhisperModel(src, device="cpu", compute_type="int8", cpu_threads=4)
    if device == "cuda":
        return WhisperModel(src, device="cuda", compute_type="float16")
    last_err = None
    for dev, comp in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel(src, device=dev, compute_type=comp)
            print(f"[asr] {src} on {dev}/{comp}", file=sys.stderr, flush=True)
            return model
        except Exception as e:          # noqa: BLE001  device fallback
            last_err = e
    raise RuntimeError(f"ASR model load failed: {last_err}")


def main():
    wav = sys.stdin.buffer.read()
    if len(wav) < 1000:
        print("[]")
        return
    try:
        import io
        model = load()
        lang = _env("VA_ASR_LANG")
        segments, _info = model.transcribe(
            io.BytesIO(wav), beam_size=1, vad_filter=True,
            language=(lang or None),
            initial_prompt=(_env("VA_ASR_PROMPT") or None))
        cues = [{"s": round(s.start, 2), "e": round(s.end, 2), "text": s.text.strip()}
                for s in segments if s.text.strip()]
        print(json.dumps(cues, ensure_ascii=False))
    finally:
        sys.stdout.flush()


if __name__ == "__main__":
    if "--probe" in sys.argv:   # panel save validation: really load, then exit
        load()
        print(json.dumps(["OK"]))
        sys.stdout.flush()
    else:
        main()
