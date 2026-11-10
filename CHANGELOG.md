# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-11-11

### Added
- **Frame gallery inside the job detail view** — "Frames the model saw": every frame the engine actually sampled, laid out as a wrapped horizontal thumbnail strip with per-frame timestamps; click a frame for full view or copy its path. Frames are pulled from the source video with ffmpeg on demand and cached next to the job ledger (up to 64 per job).
- **Pre-flight engine check** — starting an analysis now pings the LLM route first; a dead engine surfaces an actionable error instead of a silently queued job.

### Fixed
- **Panel could report "engine not ready" while analysis still worked** — the dashboard API was loaded as a second, context-less copy of the plugin, so the panel's probe never saw the wiring done at registration. Both halves now share one module instance.
- **Stale engine endpoints after switching chat models** — follow-the-chat routing now reads the host's live runtime mirror, so dynamic-port runtimes retarget analysis immediately instead of reusing a dead port cached in config.
- **Widgets rendering broken for some users** — Tailwind utility classes are only generated for selectors present in the host's compiled stylesheet, and a few classes this plugin used (notably the thumbnail height clamp) were silently dropped. Every class is now audited against the shipped CSS bundle, and sizing that cannot be guaranteed falls back to inline styles.
- **The Whisper worker was referenced but never shipped** — `engine/whisper_worker.py` is now part of the package, so the middle rung of the transcription ladder works out of the box.

## [0.1.0] - 2026-11-10

First public release.

### Added
- **Workbench panel** docked above the Hermes desktop composer: attach a video, pick a window and a preset, one click to analyze; a collapsible task list with live progress, cancel, resume and detail views.
- **Visual analysis engine**: frame sampling with real shot-cut detection (adaptive threshold), LLM observation segments fused into one full report with timeline, transcript quotes and editing suggestions.
- **Transcription, three engines behind one ladder**: local R2T2 (NetEase Youdao Confucius4-R2T2, CPU or CUDA), local Whisper, and any OpenAI-compatible cloud ASR — each with an on-machine registration + verification flow; missing engines degrade gracefully instead of failing the job.
- **Agent tools**: `video_xray` (quick single-video analysis inline) and `video_xray_jobs` (job control), plus `/video` slash command.
- **Follow-the-chat engine routing**: analysis reuses the exact model/port the chat is currently talking to (live runtime mirror), so switching models needs no reconfiguration.
- **Gentle cancellation**: workers self-exit on a cancel sentinel (VRAM released within ~1 s), the panel button shows *取消中… → 已取消*, and finished segments are kept for a one-click resume.
- **Reverse prompt export**: turn any finished analysis into a MiniMax-H3-style integrated multimodal prompt (per-shot or single-take), copy or export as Markdown + TXT.
- **i18n**: English and Simplified Chinese, following the Hermes display language; other locales fall back to English.
- **Resource guards**: configurable CPU-usage cap for local transcription, low process priority, per-engine parameter dialogs that verify by loading the engine before saving.
