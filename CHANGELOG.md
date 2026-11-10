# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
