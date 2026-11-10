"""ffmpeg/ffprobe probing + in-memory frame extraction (zero temp files,
stdlib only).

Measured (2026-11-05 P0): 512px q4 jpg ~= 155 tok/frame; 64 frames in one
request cost a prompt of ~10k tokens. Decoding runs on the CPU; the GPU is
left to vision encoding and generation (aligned with Strata).

Binary resolution: VA_FFMPEG / VA_FFPROBE env vars win; otherwise plain
names are used and resolved through PATH.
"""
import json
import os
import shutil
import subprocess

MAX_WINDOW_S = 3600.0     # single-window cap (Strata contract)
MAX_MIB = 128.0           # request size cap (Strata contract)
JPEG_M = b"\xff\xd8\xff"
JPEG_E = b"\xff\xd9"


def _bin(name, env_key):
    """Resolve an ffmpeg-family binary: env override first, then PATH."""
    return os.environ.get(env_key) or shutil.which(name) or name


def _ffmpeg():
    return _bin("ffmpeg", "VA_FFMPEG")


def _ffprobe():
    return _bin("ffprobe", "VA_FFPROBE")


def _missing_binary_hint(exc):
    if isinstance(exc, FileNotFoundError):
        raise RuntimeError(
            "ffmpeg/ffprobe not found: install ffmpeg and keep it on PATH, "
            "or set the VA_FFMPEG / VA_FFPROBE environment variables") from exc
    raise exc


def _run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        _missing_binary_hint(exc)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed rc={p.returncode}: "
                           f"{p.stderr.decode('utf-8', 'ignore')[:300]}")
    return p.stdout


def probe(path):
    """ffprobe -> {dur, width, height, has_audio, size_mib}"""
    try:
        out = subprocess.run([_ffprobe(), "-v", "quiet", "-print_format",
                              "json", "-show_format", "-show_streams", path],
                             capture_output=True, text=True,
                             encoding="utf-8", timeout=60).stdout
    except FileNotFoundError as exc:
        _missing_binary_hint(exc)
    try:
        d = json.loads(out)
    except ValueError:
        raise ValueError(f"ffprobe returned no parsable info for {path!r} "
                         "(corrupt or unsupported media file?)")
    v = next((s for s in d.get("streams", [])
              if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError(f"no video stream in {path!r} "
                         "(audio-only file or a plain image?)")
    dur = float(d["format"].get("duration") or v.get("duration") or 0)
    size_mib = int(d["format"].get("size", os.path.getsize(path))) / 1048576.0
    if dur <= 0:
        raise ValueError("video duration probe failed (0s)")
    if dur > MAX_WINDOW_S:
        raise ValueError(f"window {dur:.0f}s exceeds the "
                         f"{MAX_WINDOW_S:.0f}s cap; split the analysis with "
                         f"start/end seconds")
    return {"dur": dur, "width": int(v["width"]), "height": int(v["height"]),
            "has_audio": any(s.get("codec_type") == "audio"
                             for s in d["streams"]),
            "size_mib": size_mib}


def _split_jpegs(blob):
    """Split frames out of an image2pipe byte stream at JPEG SOI/EOI bounds."""
    frames, i = [], 0
    while True:
        s = blob.find(JPEG_M, i)
        if s < 0:
            break
        e = blob.find(JPEG_E, s)
        if e < 0:
            break
        frames.append(blob[s:e + 2])
        i = e + 2
    return frames


def _scene_times(path, start, end, thresh=0.2):
    """ffmpeg scene detection: times of "significant picture changes" inside
    the window (already de-densified)."""
    try:
        p = subprocess.run([_ffmpeg(), "-v", "info", "-ss", f"{start:.3f}",
                            "-t", f"{end - start:.3f}", "-i", path,
                            "-vf", f"select='gt(scene,{thresh})',showinfo",
                            "-an", "-sn", "-f", "null", "-"],
                           capture_output=True, timeout=300)
    except FileNotFoundError as exc:
        _missing_binary_hint(exc)
    import re
    times = [float(m.group(1)) + start
             for m in re.finditer(rb"pts_time:([0-9.]+)", p.stderr)]
    out = []
    for t in sorted(times):
        if not out or t - out[-1] >= 0.7:      # bursts within 0.7s = one hit
            out.append(min(t, end - 0.05))
    return out


def _extract_at(path, times, scale):
    """Extract one exact frame per timestamp (adaptive-only; <=64 frames is
    an acceptable number of ffmpeg invocations)."""
    out = []
    for t in times:
        try:
            blob = _run([_ffmpeg(), "-v", "error", "-ss", f"{t:.3f}",
                         "-i", path, "-frames:v", "1",
                         "-vf", f"scale={scale}:-2",
                         "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4",
                         "-an", "pipe:1"], timeout=60)
        except RuntimeError:
            continue
        fr = _split_jpegs(blob)
        if fr:
            out.append((t, fr[0]))
    return out


def _sample_adaptive(path, start, end, fps, max_frames, scale):
    """Uniform base grid guaranteeing full coverage + densification around
    scene changes (Strata adaptive semantics)."""
    window = end - start
    base_n = max(2, min(max_frames, int(window * fps), int(max_frames * 0.5)))
    times = [start + (i + 0.5) / base_n * window for i in range(base_n)]
    budget = max_frames - len(times)
    if budget > 0:
        extra = []
        for t in _scene_times(path, start, end):
            for dt in (-0.4, -0.1, 0.3):        # two pre-frames + one post
                tt = t + dt
                if start <= tt <= end - 0.05:
                    extra.append(tt)
        extra.sort()
        keep, last = [], -9
        for tt in extra:                        # no re-sample within 0.25s
            if tt - last >= 0.25 and len(keep) < budget:
                keep.append(tt)
                last = tt
        times = sorted(times + keep)
    frames = _extract_at(path, times, scale)
    if not frames:
        raise RuntimeError("adaptive sampling produced no frames")
    return frames


def sample(path, start=0.0, end=None, fps=8.0, max_frames=64, scale=512,
           strategy="uniform", probe_info=None):
    """Sample frames. Returns ([(pts, jpg_bytes)], probe_info).

    uniform:  single-pass pipe, evenly spaced (fast).
    adaptive: uniform base grid + densification around ffmpeg scene-change
              points (CPU decode; GPU left to vision encoding and generation
              -- aligned with Strata).
    """
    info = probe_info or probe(path)
    end = info["dur"] if end is None else min(float(end), info["dur"])
    start = max(0.0, float(start))
    if start >= end:
        # a start past the video (or past end) used to collapse into a ~0s
        # window and die as a cryptic ffmpeg EINVAL; fail with a clear message
        raise ValueError(
            f"start {start:.1f}s is past the analysable end {end:.1f}s "
            f"(video is {info['dur']:.1f}s long)")
    window = min(end - start, MAX_WINDOW_S)
    fps = max(0.01, float(fps))
    if window * fps > max_frames:                       # density adapts
        fps = max_frames / window
    if strategy == "adaptive":
        frames = _sample_adaptive(path, start, end, fps, max_frames, scale)
        return frames, info
    n = max(2, min(max_frames, int(window * fps)))
    eff_fps = n / window
    blob = _run([_ffmpeg(), "-v", "error", "-ss", f"{start:.3f}",
                 "-t", f"{window:.3f}", "-i", path,
                 "-vf", f"fps={eff_fps:.6f},scale={scale}:-2",
                 "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4",
                 "-an", "pipe:1"], timeout=300)
    frames = _split_jpegs(blob)
    if not frames:
        raise RuntimeError("no frames extracted (unsupported codec?)")
    out = [(start + (i + 0.5) / len(frames) * window, f)
           for i, f in enumerate(frames)]
    return out, info


def audio_pcm16k(path, start=0.0, end=None, probe_info=None, timeout=300,
                 stream=0, channel=0):
    """Audio track -> 16k mono s16 PCM bytes (in memory). None if no track.

    stream = which audio stream to map (multi-track pro sources; 0 = the
    first audio stream). channel = 0 default downmix, 1 take the left
    channel, 2 take the right — for dual-language stereo sources that
    pack two languages into one stream. An out-of-range stream makes
    ffmpeg fail, which the pipeline already degrades gracefully."""
    info = probe_info or probe(path)
    if not info["has_audio"]:
        return None
    end = info["dur"] if end is None else min(float(end), info["dur"])
    start = max(0.0, float(start))
    if start >= end:
        return None                      # empty window: nothing to transcribe
    end = min(end, start + MAX_WINDOW_S)
    cmd = [_ffmpeg(), "-v", "error", "-ss", f"{start:.3f}",
           "-t", f"{end - start:.3f}", "-i", path,
           "-vn", "-map", f"0:a:{int(stream)}"]
    if int(channel) in (1, 2):           # pan keeps the mono 16k contract
        cmd += ["-af", f"pan=mono|c0=c{int(channel) - 1}"]
    else:
        cmd += ["-ac", "1"]
    out = _run(cmd + ["-ar", "16000", "-f", "s16le", "-c:a", "pcm_s16le",
                      "pipe:1"], timeout=timeout)
    return out or None
