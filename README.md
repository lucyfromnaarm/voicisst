# Voicisst

Free, open-source voice dictation. Speak naturally — Voicisst turns it into clear, polished writing in every app.

[![CI](https://github.com/lucyfromnaarm/voicisst/actions/workflows/ci.yml/badge.svg)](https://github.com/lucyfromnaarm/voicisst/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lucyfromnaarm/voicisst)](https://github.com/lucyfromnaarm/voicisst/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

Hold a key, talk, let go. Whisper transcribes your speech locally, a small LLM reworks the rambling into clean written text, and Voicisst types or pastes the result into whatever window has focus.

## Why Voicisst

I have ME/CFS. On bad days typing is expensive and talking is cheap, and I got tired of dictation tools that want a subscription, an account, and a copy of my voice on someone else's servers. So I built the tool I needed: no subscription, no cloud, no telemetry. Your audio never leaves machines you control, and the whole thing is MIT-licensed.

## What it does

- Works in any app. Notion, Gmail, Google Docs, WhatsApp, Cursor, terminals — anything that takes keyboard input. Voicisst detects terminals and hands you the text on the clipboard instead of risking a paste that terminals would mangle.
- Cleans up your speech, not just the words. Fillers go ("um", "you know"), self-corrections resolve ("at 2... actually 3" becomes "at 3"), spoken lists ("first X, second Y") become numbered Markdown lists, and punctuation lands where it belongs.
- Spells names right. A personal dictionary plus (on Linux) whatever text you have highlighted feed both Whisper and the polisher, so "Anja" stops coming out as "Anya".
- Speaks your language. Whisper auto-detects 100+ languages and the polish step keeps the output in the language you spoke.
- Hears a whisper. RMS normalization boosts quiet speech before transcription, so you can dictate without waking the house.
- Shows you it's listening. A small on-screen waveform moves with your voice while you dictate (and names the mic it's using), so you never wonder whether the hotkey took.
- Two ways to talk: hold-to-talk or tap-to-toggle, with optional silence auto-stop for fully hands-free use. Every hotkey is configurable.
- Live typing, if you want it. Streaming mode types the raw transcript while you're still speaking, then swaps it for the polished version.
- Transcribes recordings too. Drop an audio file into the web UI, or run `voicisst transcribe-file recording.m4a --output notes.md`; long files are chunked automatically.
- Runs fully local on any decent GPU — or split it: heavy models on the big computer (`voicisst serve`), a featherweight client on the laptop (`voicisst run --server`).

## Quickstart

You need a working microphone, and [Ollama](https://ollama.com) if you want the LLM polish step (you do — but `polish.backend = "none"` works fine without it).

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/lucyfromnaarm/voicisst/main/scripts/install.sh | bash
```

or with pipx / uv:

```bash
pipx install "voicisst[local,ui,media]"  # or: uv tool install "voicisst[local,ui,media]"
ollama pull qwen3.5:4b                   # the default polish model
voicisst selftest                            # checks mic, models, hotkeys, typing
voicisst run                                 # (bare `voicisst` does the same)
```

On Linux, run `scripts/setup-linux.sh` once first — it installs ydotool, sets up the uinput permissions and the `input` group, and installs user systemd units. Details in [docs/PLATFORMS.md](docs/PLATFORMS.md).

**Windows**

```powershell
irm https://raw.githubusercontent.com/lucyfromnaarm/voicisst/main/scripts/install.ps1 | iex
```

or `pipx install "voicisst[local,ui,media]"`, then `voicisst selftest` and `voicisst run` as above.

**Prebuilt binaries**

No Python needed: grab `voicisst-<version>-<os>-<arch>.tar.gz` (or `.zip`) from the [Releases page](https://github.com/lucyfromnaarm/voicisst/releases), unpack, run `./voicisst`.

Then hold the hotkey — Menu key on Linux, right Option on macOS, right Ctrl on Windows — speak, and release.

### Prefer buttons to config files?

```bash
voicisst ui
```

That opens a settings page in your browser: a setup wizard that helps you pick a microphone, capture a hotkey by pressing it, and check your models — then writes the config file for you. `voicisst run --ui` adds a live dashboard that shows what dictation is doing right now (listening, transcribing, polishing...), so every audio cue has a visual equivalent and you never need sound to know the state. The page runs only on your own machine and makes no external requests. Details in [docs/UI.md](docs/UI.md).

### Transcribe a recording

```bash
voicisst transcribe-file recording.m4a --output transcript.md
```

The command uses the same Whisper and polish settings as dictation, but writes
the cleaned text instead of typing into another app. M4A/AAC works with the
`media` extra (`pip install "voicisst[media]"`) or `ffmpeg` on your PATH; the
install scripts and quickstart extras include `media`.

## Three ways to run it

```
all-in-one: voicisst run                      split: voicisst serve  +  voicisst run --server URL

+----------------------------+        +----------------+          +-------------------+
|        one machine         |        |     laptop     | HTTP/WS  |  desktop with GPU |
|  mic -> Whisper -> LLM     |        | mic, hotkeys,  | <------> |  Whisper + LLM    |
|         -> focused window  |        | typing         |          |  (voicisst serve)     |
+----------------------------+        +----------------+          +-------------------+
```

1. **All-in-one** — `voicisst run` (or just `voicisst`): capture, transcribe, polish, and inject, all on one machine. Use `voicisst run --ui` when you also want the live browser dashboard.
2. **Server** — `voicisst serve` on the GPU box exposes transcription and polish over HTTP + WebSocket.
3. **Client** — `voicisst run --server URL` on the laptop: audio capture and typing stay local, inference happens on the server.

```bash
voicisst serve --host 0.0.0.0 --token <secret>            # on the big machine
voicisst run --server http://big-box:8765 --token <secret> # on the laptop
```

Server setup, the API, and the security model are in [docs/SERVER.md](docs/SERVER.md).

## Requirements

- A GPU is recommended: the defaults (Whisper `large-v3-turbo` plus a ~4B polish model) fit comfortably in about 8 GB of VRAM.
- CPU-only works too: Voicisst auto-selects the `small` Whisper model on CPU.
- Polish needs [Ollama](https://ollama.com) (`ollama pull qwen3.5:4b`) or any OpenAI-compatible server (llama.cpp, vLLM, LM Studio, ...) — or set `polish.backend = "none"` to skip it entirely.
- Python 3.10+ for pip/pipx installs. The release binaries bundle everything.

## Configuration

`voicisst config init` writes a documented `config.toml`; `voicisst config path` tells you where it lives. A taste:

```toml
[hotkey]
keys = ["KEY_F9"]           # any key you like
mode = "toggle"             # tap to start, tap to stop

[audio]
auto_stop_silence_s = 2.0   # hands-free: stop after 2s of silence

[replacements]
"vs code" = "VS Code"
```

Every option, every default, the `VOICISST_*` environment variables, and the dictionary format: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Privacy

Voicisst has no accounts, no telemetry, and no cloud component. In local mode nothing touches the network except your own Ollama instance on localhost. In client/server mode audio travels only between your client and your server, with optional bearer-token auth — both ends are machines you own. Dictation history is off by default; if you turn it on, it's a local file you can delete.

## Accessibility

Voicisst exists because of a chronic illness, and it's built for limited energy and mobility first: toggle mode plus silence auto-stop means dictating without holding anything down, every hotkey and threshold is configurable, audio cues confirm state without needing to look, and the setup scripts try hard to leave nothing manual. Every audio cue also has a visual equivalent — the on-screen overlay shows each dictation state by motion as well as color, and the dashboard and tray icon show it by shape and text, never color alone — and the web UI is built to WCAG AA, keyboard-first. If something about Voicisst is hard to use with your body, that's a bug — [please open an issue](https://github.com/lucyfromnaarm/voicisst/issues).

## Docs

- [The web UI: setup wizard, settings, dashboard, and file transcription](docs/UI.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Running a server](docs/SERVER.md)
- [Platform setup (Linux / macOS / Windows)](docs/PLATFORMS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## Contributing

Bug reports, backends for new compositors, and accessibility feedback are all welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) — `SPEC.md` is the architecture contract, and the test suite runs fully headless.

## License

[MIT](LICENSE).
