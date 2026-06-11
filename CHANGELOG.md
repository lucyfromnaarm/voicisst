# Changelog

All notable changes to Flow are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-11

First release. Flow grew out of a single-file Linux prototype; 0.1.0 is the
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
- Hybrid thinking mode for reasoning models (skipped below a configurable
  input length) and a VRAM watchdog that unloads the polish model when free
  GPU memory runs low.
- Output injection: ydotool (Wayland), xdotool (X11), pynput
  (macOS/Windows); paste or per-keystroke type modes, Shift+Enter newline
  handling for chat apps, terminal detection with copy + notify instead of
  pasting.
- Streaming mode: live-types the raw transcript while you speak, shows a
  processing indicator, then replaces it in place with the polished text;
  Backspace during polish cancels and keeps the raw transcript.
- Client/server split: `flow serve` exposes REST + WebSocket endpoints
  (`/v1/health`, `/v1/transcribe`, `/v1/polish`, `/v1/process`,
  `/v1/stream`) with bearer-token auth; `flow run --server URL` keeps
  capture and typing local.
- Audio pipeline: RMS normalization for quiet/whispered speech, muted-mic
  detection, silence gate, min/max recording bounds, optional silence
  auto-stop for hands-free use, audio cues.
- Personal dictionary (config list + `dictionary.txt`) and, on Linux,
  primary-selection context for correct spelling of names and jargon.
- Post-polish find/replace rules (case-insensitive, whole-word).
- Configuration via TOML + `FLOW_*` environment variables + CLI flags, with
  the prototype's legacy env vars still honored; `flow config
  init/show/path`.
- `flow selftest` environment diagnostics (config, hotkeys, audio capture,
  engine health, injection, clipboard, polish round-trip).
- Optional dictation history (local JSONL) and optional tray icon.
- Packaging: PyPI extras (`local`, `server`, `tray`, `all`), PyInstaller
  binaries for Linux/macOS/Windows, install scripts, Linux setup script
  (udev/uinput/input group/ydotoold), systemd units, macOS LaunchAgent
  template.

[Unreleased]: https://github.com/lucyfromnaarm/flow-dictation/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lucyfromnaarm/flow-dictation/releases/tag/v0.1.0
