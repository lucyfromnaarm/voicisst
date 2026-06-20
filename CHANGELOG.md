# Changelog

All notable changes to Voicisst are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Dictation overlay**: a small dark pill at the bottom of the screen while
  you dictate — a live waveform that moves with your voice, with the mic's
  name shown for the first couple of seconds. Transcribing, polishing and
  "delivered" each get their own motion and color (never color alone). Built
  on stdlib tkinter, so it needs no extra; on by default (`[ui] overlay`,
  `--no-overlay`, or the Settings page turn it off). It never takes focus,
  pins to the primary monitor on multi-screen setups, and quietly steps
  aside on machines where it can't run (headless, macOS for now).
- **LM Studio backend** (`polish.backend = "lmstudio"`): polish through LM
  Studio's built-in local server. Picking it is enough — if `polish.url` is
  still the Ollama default, Voicisst uses LM Studio's `http://localhost:1234`
  on its own, and the web UI swaps the address field for you.
- **Model dropdown**: the polish model fields in Settings and the setup
  wizard now list the models already installed on your backend (Ollama or
  LM Studio), fetched from a new local `/api/polish/models` endpoint. You
  can still type any model name.
- **Audio file transcription**: `voicisst transcribe-file` and the web UI's
  Files page turn recordings into cleaned-up text. Long files are chunked
  before transcription, and M4A/AAC works through PyAV or `ffmpeg`.

### Changed

- The tray icon now mirrors the live dictation state whenever the overlay
  or the web UI is on (previously only with `--ui`).
- **Shorter settings page**: each section now shows only the settings most
  people touch; the rest sit behind a closed "More options" disclosure. The
  polish API key field only appears for the `openai` backend.

## [0.2.0] - 2026-06-11

A web UI, so nobody has to edit TOML by hand unless they want to.

### Added

- **Web UI** (`voicisst ui`): setup wizard, settings editor, and help, served
  on `127.0.0.1` only and opened in your normal browser. No build step, no
  frameworks, no external requests; every page load needs a per-run secret
  token, so other programs and users on the machine can't reach it either.
  Installed via the new `ui` extra (`pip install "voicisst[ui]"`).
- **Setup wizard**: pick and test your microphone, capture a hotkey by
  pressing it, choose hold or toggle mode, set up local or remote
  transcription with a warm-up check, test the polish step, and get
  platform permission instructions for your OS only. Every step can be
  skipped, and the whole wizard can be rerun from Help.
- **Settings editor**: forms for every config option plus a raw TOML editor.
  Both validate before saving, and saving keeps the comments and layout of
  your config file.
- **Live dashboard** (`voicisst run --ui`): a large indicator showing what
  dictation is doing right now — idle, listening, transcribing, polishing,
  delivering, or error — using shape and text as well as color, with
  updates pushed over a WebSocket. Screen readers get the same updates
  through a live region.
- **State-aware tray icon**: with `voicisst run --ui --tray`, the tray icon
  changes shape and color with the dictation state (distinguishable without
  color vision) and gains an "Open settings UI" menu item.
- `events.StateBus`: a small thread-safe publish/subscribe bus that carries
  dictation state to the tray and the dashboard without being able to break
  dictation itself.
- New `[ui]` config keys: `web_port` (default `8766`) and `open_browser`
  (default `true`); `voicisst ui --port/--no-browser` override them.

## [0.1.0] - 2026-06-11

First release. Voicisst grew out of a single-file Linux prototype; 0.1.0 is the
cross-platform rewrite.

### Added

- Hold-to-talk and toggle dictation with configurable hotkeys; evdev backend
  on Linux, pynput on macOS/Windows (and as a Linux fallback).
- Local transcription with faster-whisper: auto model/device selection
  (`large-v3-turbo` on CUDA, `small` on CPU), auto language detection across
  100+ languages, per-utterance vocabulary via `initial_prompt`.
- LLM polish through Ollama or any OpenAI-compatible server: removes
  fillers, applies self-corrections ("at 2... actually 3" → "at 3"), turns
  spoken enumerations into numbered Markdown lists, punctuates, and answers
  in the input language. Falls back to the raw transcript on any failure.
- Opt-in thinking mode for reasoning models (off by default; skipped below a
  configurable input length even when on) and a VRAM watchdog that unloads
  the polish model when free GPU memory runs low.
- Output injection: ydotool (Wayland), xdotool (X11), pynput
  (macOS/Windows); paste or per-keystroke type modes, Shift+Enter newline
  handling for chat apps, terminal detection with copy + notify instead of
  pasting.
- Streaming mode: live-types the raw transcript while you speak, shows a
  processing indicator, then replaces it in place with the polished text;
  Backspace during polish cancels and keeps the raw transcript.
- Client/server split: `voicisst serve` exposes REST + WebSocket endpoints
  (`/v1/health`, `/v1/transcribe`, `/v1/polish`, `/v1/process`,
  `/v1/stream`) with bearer-token auth; `voicisst run --server URL` keeps
  capture and typing local.
- Audio pipeline: RMS normalization for quiet/whispered speech, muted-mic
  detection, silence gate, min/max recording bounds, optional silence
  auto-stop for hands-free use, audio cues.
- Personal dictionary (config list + `dictionary.txt`) and, on Linux,
  primary-selection context for correct spelling of names and jargon.
- Post-polish find/replace rules (case-insensitive, whole-word).
- Configuration via TOML + `VOICISST_*` environment variables + CLI flags, with
  the prototype's legacy env vars still honored; `voicisst config
  init/show/path`.
- `voicisst selftest` environment diagnostics (config, hotkeys, audio capture,
  engine health, injection, clipboard, polish round-trip).
- Optional dictation history (local JSONL) and optional tray icon.
- Packaging: PyPI extras (`local`, `server`, `tray`, `all`), PyInstaller
  binaries for Linux/macOS/Windows, install scripts, Linux setup script
  (udev/uinput/input group/ydotoold), systemd units, macOS LaunchAgent
  template.

[Unreleased]: https://github.com/lucyfromnaarm/voicisst/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/lucyfromnaarm/voicisst/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lucyfromnaarm/voicisst/releases/tag/v0.1.0
