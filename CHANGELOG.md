# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.2] - 2026-11-17

### Fixed

- **Chinese (and all CJK) transcriptions turned to mojibake after the first
  3 minutes** — the ASR worker printed its UTF-8 transcript into a stdout
  pipe that Windows reinterpreted as Windows-1252, so every line past the
  first segment arrived as Latin garbage ("Èø½s" for 这). The engine now
  forces UTF-8 on both sides of the pipe (child env `PYTHONUTF8=1` +
  `reconfigure(encoding="utf-8")`). Verified on a real 13-minute clip:
  0 mojibake lines out of 92, 407 CJK chars intact; a mixed zh+en sample
  round-trips 43/43 lines.
- **Very short audio (<0.98 s) no longer aborts the whole transcription
  job.** The VAD model physically cannot emit a segment under ~1 s, so the
  worker now blind-transcribes that audio once and continues.
- **Missing VAD weights are named in plain words.** Mounting a weights
  folder without the VAD trio used to fail with an internal "vad_onnx
  required" error; the panel now says which files are missing and where to
  get them.
- **Radio-row ghost clicks** — clicking the golden download button while
  the settings dialog was already open could re-toggle the engine radio
  underneath; rows are now inert while the dialog is up and the engine
  selection survives every reload.
- **H3 prompt-reverse and long exports are no longer killed at a fixed
  180 s**, matching the 21-minute cap the panel already waits for.

### Changed

- Engine selection and saved forms are written to one live source of
  truth the moment you touch them — restarting Hermes no longer reverts
  the panel to a stale snapshot.
- The finished-jobs strip truncates its own title instead of letting a
  long line overlap it (full text on hover).

## [0.3.1] - 2026-11-17

### Fixed

- **CUDA gate rejection no longer fails with HTTP 500** — the "install the CPU
  pack first" rejection carried a literal double-brace artifact in its reason
  text, which crashed the server with `TypeError: cannot use 'dict' as a set
  element` instead of showing the friendly notice.
- **Downloads no longer abort with a misleading "every source unavailable"
  error** — a progress/log line inside the downloader referenced a variable
  that does not exist in its scope; the exception fired on the *success* path
  and was swallowed by the network-retry handler, so healthy downloads looked
  like network failures. Fixed properly and verified end-to-end with a real
  50 MB release download plus SHA-256 check.

### Changed

- The install banner in the panel (and the README) now spells out the required
  order for NVIDIA users: **install the CPU pack first, then the CUDA
  add-on — order matters**. The previous wording left first-time users free to
  click the CUDA button first and hit the gate.

## [0.3.0] - 2026-11-17

### Added

- **One-click engine installers (CPU / CUDA / Whisper)** — the panel can now
  download and unpack the speech-to-text runtime engines themselves from this
  repository's GitHub Releases assets (with mirror fallback and SHA-256
  verification). Engines land in `asr-r2t2/` / `asr-whisper/` under the Hermes
  home directory and Whisper auto-registers itself after a successful install.
  A global install lock prevents concurrent installs; staging happens under
  the user's own `%TEMP%\video-xray-install\` and both the zip and the stage
  directory are removed once unpacked.
- **Model-weights download wizard** — golden "download weights" button opens a
  guided wizard for the R2T2 GGUF weights (official `netease-youdao` repos,
  q4/q8 tiers), the FireRedVAD ONNX bundle and the faster-whisper sizes
  (official `Systran` repos). Sources are tried mirror-first
  (hf-mirror.com, ModelScope) with huggingface.co as fallback; downloads are
  resumable (`.part` across mirrors), SHA-pinned, and cancel at file
  boundaries.
- **Machine-readable error codes (i18n protocol)** — every download/install
  failure now carries a stable code (`reason_code` / `error_code`) plus an
  English technical detail instead of a locale-baked sentence, so the panel
  translates failures into the UI language at display time. A switch to an
  English panel can no longer show Chinese error text (and vice versa).
- **Load-directory gating for "Analyze now"** — the run button stays locked
  (with a hover list of every missing prerequisite, including a busy state)
  until the engine is ready *and* a model directory is actually loaded;
  leaving the path field empty in the parameter dialog materializes the
  per-engine default directory on save, so the lock can never deadlock.

- **Action logging inside the plugin directory + an 「Open log」 button** —
  installs, downloads and transcriptions now leave single-line start/
  success/failure traces in `plugins/video-xray/logs/video-xray.log`
  (rotation, English). The panel gained a one-click button that opens that
  file with the system default viewer, so users can read full failure
  traces any time — and know which file to attach when reporting one.

### Changed

- Clearer button wording: "Load model directory & set parameters",
  "Select a local speech-to-text engine directory"; the parameter button
  keeps a permanent amber accent so its disabled state stays visible.
- Auto engine mode no longer skips the model-directory check — with no
  usable local engine and no ready cloud engine, "Analyze now" stays locked.
- Hover tooltips on disabled buttons are restored by re-enabling pointer
  events locally (the host button style disables pointer events on
  `:disabled`, which had been swallowing the tooltips).

### Fixed

- Busy-state hover showed a raw i18n key (`run_locked`) instead of text —
  the key was missing from both language books.
- The dashboard `/asr/download/start` route re-wrapped engine rejections as
  HTTP 400 with a locale-baked message, swallowing the new `reason_code`
  protocol; rejections now pass through as structured results like every
  other route.
- Post-install auto-registration checked the wrong result key
  (`registered` instead of `ok`), so a successful Whisper install reported a
  registration warning.

## [0.2.2] - 2026-11-13

### Fixed

- **Cloud ASR key sources trimmed (catalog review, round 2)** — the duplicate raw
  `os.environ.get("VA_ASR_CLOUD_KEY")` read is gone (`agent.secret_scope.get_secret`
  already resolves that variable, profile-scoped), and the
  `video-xray.asr.cloud.api_key` config.yaml fallback was removed: keys now live
  only in the secret store (.env) or the panel-saved 0600 registry, so a shared
  config file can never carry one. Panel hint text updated to match.
- **Registry temp file created 0600** — `asr-engines.json.tmp` is now created via
  `os.open(..., 0o600)` so a plaintext key is never world-readable between create
  and chmod.

## [0.2.1] - 2026-11-12


### Fixed

- **ASR worker subprocesses no longer inherit the whole gateway environment** —
  they picked up unrelated provider secrets (and the harness could hijack a
  user venv's packages). Workers now receive a minimal, audited variable set
  plus their own VA_* overrides.
- **Cloud ASR key handling** — the silent `OPENAI_API_KEY` fallback is gone (it
  shipped the LLM provider's key to whatever base_url a user configured). The
  key now resolves from `VA_ASR_CLOUD_KEY`, the panel-saved registry, the
  official secret store (`agent.secret_scope.get_secret`), or the explicitly
  scoped `video-xray.asr.cloud.api_key` config entry, in that order.
- **Job ids are validated before touching the filesystem** — strict
  `[A-Za-z0-9._-]{1,128}` whitelist (rejecting `..`) on every ledger path
  built from REST/CLI input, closing a path-traversal hole.
- **All plugin state moved to `<HERMES_HOME>/plugin-data/video-xray/`** — job
  ledger, R2T2/Whisper/cloud settings and the cloud key file (written 0600).
  HERMES_HOME is resolved through `hermes_constants.get_hermes_home()`
  (profile-aware) instead of `parents[3]` guesses; existing state migrates
  automatically on first start.
- **Dropped the private `registered_tasks` core import** — the dashboard probe no
  longer reaches into host internals that may not exist upstream.
- **Removed the unused `engine/whisper_worker.py`** — the transcription workers
  that actually run are `engine/asr_worker.py` (Whisper/cloud) and the R2T2
  runtime worker; the stale duplicate and its hf-mirror default are gone.

### Changed

- `provides_tools` in `plugin.yaml` is now a list of tool names instead of a count.


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
