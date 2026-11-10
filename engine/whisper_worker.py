# -*- coding: utf-8 -*-
"""whisper worker v2 — auto model resolution with snapshot dirs, HF cache
and on-demand download (hf-mirror default for users without a VPN).

stdin : 16 kHz mono 16-bit PCM WAV
stdout: RIFF WAV on --stream mode; JSON lines otherwise
args  : --model small --dir <weights_dir> | --model-dir <resolved_dir>
        --device auto|cpu|cuda --language <BCP-47|auto>
"""
import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def _snap_ok(p):
    try:
        return ((p / "model.bin").stat().st_size > 10_000_000
                and (p / "tokenizer.json").exists())
    except OSError:
        return False


def _find_snapshot(root, name):
    for hub in (root / "models--Systran--faster-whisper-%s" % name,
                Path.home() / ".cache" / "huggingface" / "hub"
                / ("models--Systran--faster-whisper-%s" % name)):
        snaps = hub / "snapshots"
        try:
            for s in sorted(snaps.iterdir(), reverse=True):
                if _snap_ok(s):
                    return s
        except OSError:
            pass
    return None


def _resolve(model, weights_dir):
    """Returns (load_target, download_root_or_None)."""
    if weights_dir:
        d = Path(weights_dir)
        if _snap_ok(d):
            return str(d), None
        snap = _find_snapshot(d, model)
        if snap:
            return str(snap), None
        return model, str(d)          # not there yet: auto-download into dir
    snap = _find_snapshot(Path.home() / ".cache" / "huggingface" / "hub", model)
    if snap:
        return str(snap), None
    return model, None                # faster-whisper default cache


def _wav_frames(raw):
    try:
        with wave.open(__import__("io").BytesIO(raw), "rb") as wf:
            n = wf.getnframes()
            if n <= 0:
                return 0.0
            return n / float(wf.getframerate())
    except Exception:
        return None


def _stream_emit(pcm, sr):
    import struct
    hdr = bytearray(b"RIFF")
    hdr += struct.pack("<I", 36 + len(pcm))
    hdr += b"WAVEfmt "
    hdr += struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(pcm))
    sys.stdout.buffer.write(bytes(hdr) + pcm)
    sys.stdout.buffer.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--stream", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdin.reconfigure(errors="surrogateescape")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    raw = sys.stdin.buffer.read()
    if not raw.strip():
        print("[]")
        return 0
    dur = _wav_frames(raw)

    def log(msg):
        print("R2T2-WHISPER %s" % msg, file=sys.stderr, flush=True)

    t0 = time.time()
    if args.model_dir:
        target, root = args.model_dir, None
    else:
        target, root = _resolve(args.model, args.dir)
    log("load start model=%s target=%s download=%s t=%.1fs" %
        (args.model, target, bool(root), time.time() - t0))
    from faster_whisper import WhisperModel
    kw = {} if root is None else {"download_root": root}
    model = None
    for dev, ct in ((args.device, "float16"),
                    ("cpu", "int8")):
        try:
            model = WhisperModel(target, device=dev, compute_type=ct, **kw)
            break
        except Exception:
            continue
    if model is None:
        raise RuntimeError("faster-whisper could not load model (%s)" % target)
    log("load done t=%.1fs" % (time.time() - t0))

    kw2 = {"vad_filter": True, "word_timestamps": True}
    if args.language and args.language != "auto":
        kw2["language"] = args.language.split("-")[0].lower()
    segs, info = model.transcribe(raw, beam_size=5, **kw2)
    out = []
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        cues = [{"start": round(c.start, 3), "end": round(c.end, 3),
                 "text": c.word} for c in (s.words or []) if (c.word or "").strip()]
        out.append({"start": round(s.start, 3), "end": round(s.end, 3),
                    "text": text, "cues": cues})
        log("segment %d end=%.1fs t=%.1fs" % (len(out), s.end, time.time() - t0))
    if dur is not None:
        if out:
            out[-1]["end"] = round(min(out[-1]["end"], dur), 3)
        for i in range(1, len(out)):
            if out[i]["start"] < out[i - 1]["end"]:
                out[i]["start"] = out[i - 1]["end"]
    log("done segs=%d t=%.1fs" % (len(out), time.time() - t0))
    if args.stream:
        _stream_emit(raw, 16000)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
