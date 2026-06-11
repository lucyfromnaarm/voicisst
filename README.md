# Flow

Free, open-source voice dictation. Speak naturally — Flow turns it into clear, polished writing in every app.

[![CI](https://github.com/lucyfromnaarm/flow-dictation/actions/workflows/ci.yml/badge.svg)](https://github.com/lucyfromnaarm/flow-dictation/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lucyfromnaarm/flow-dictation)](https://github.com/lucyfromnaarm/flow-dictation/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

Hold a key, talk, let go. Whisper transcribes your speech locally, a small LLM reworks the rambling into clean written text, and Flow types or pastes the result into whatever window has focus.

## Why Flow

I have ME/CFS. On bad days typing is expensive and talking is cheap, and I got tired of dictation tools that want a subscription, an account, and a copy of my voice on someone else's servers. So I built the tool I needed: no subscription, no cloud, no telemetry. Your audio never leaves machines you control, and the whole thing is MIT-licensed.

## What it does

- Works in any app. Notion, Gmail, Google Docs, WhatsApp, Cursor, terminals — anything that takes keyboard input. Flow detects terminals and hands you the text on the clipboard instead of risking a paste that terminals would mangle.
- Cleans up your speech, not just the words. Fillers go ("um", "you know"), self-corrections resolve ("at 2... actually 3" becomes "at 3"), spoken lists ("first X, second Y") become numbered Markdown lists, and punctuation lands where it belongs.
- Spells names right. A personal dictionary plus (on Linux) whatever text you have highlighted feed both Whisper and the polisher, so "Anja" stops coming out as "Anya".
- Speaks your language. Whisper auto-detects 100+ languages and the polish step answers in the language you spoke.
- Hears a whisper. RMS normalization boosts quiet speech before transcription, so you can dictate without waking the house.
- Two ways to talk: hold-to-talk or tap-to-toggle, with optional silence auto-stop for fully hands-free use. Every hotkey is configurable.
- Live typing, if you want it. Streaming mode types the raw transcript while you're still speaking, then swaps it for the polished version.
- Runs fully local on any decent GPU — or split it: heavy models on the big computer (`flow serve`), a featherweight client on the laptop (`flow run --server`).

## Quickstart

You need a working microphone, and [Ollama](https://ollama.com) if you want the LLM polish step (you do — but `polish.backend = "none"` works fine without it).

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/lucyfromnaarm/flow-dictation/main/scripts/install.sh | bash
```

or with pipx / uv:

```bash
pipx install "flow-dictation[local]"     # or: uv tool install "flow-dictation[local]"
ollama pull qwen3.5:4b                   # the default polish model
flow selftest                            # checks mic, models, hotkeys, typing
flow
```

On Linux, run `scripts/setup-linux.sh` once first — it installs ydotool, sets up the uinput permissions and the `input` group, and installs user systemd units. Details in [docs/PLATFORMS.md](docs/PLATFORMS.md).

**Windows**

```powershell
irm https://raw.githubusercontent.com/lucyfromnaarm/flow-dictation/main/scripts/install.ps1 | iex
```

**Prebuilt binaries**

No Python needed: grab `flow-<version>-<os>-<arch>.tar.gz` (or `.zip`) from the [Releases page](https://github.com/lucyfromnaarm/flow-dictation/releases), unpack, run `./flow`.

Then hold the hotkey — Menu key on Linux, right Option on macOS, right Ctrl on Windows — speak, and release.

## Three ways to run it

```
all-in-one: flow run                      split: flow serve  +  flow run --server URL

+----------------------------+        +----------------+          +-------------------+
|        one machine         |        |     laptop     | HTTP/WS  |  desktop with GPU |
|  mic -> Whisper -> LLM     |        | mic, hotkeys,  | <------> |  Whisper + LLM    |
|         -> focused window  |        | typing         |          |  (flow serve)     |
+----------------------------+        +----------------+          +-------------------+
```

1. **All-in-one** — `flow run` (or just `flow`): capture, transcribe, polish, and inject, all on one machine.
2. **Server** — `flow serve` on the GPU box exposes transcription and polish over HTTP + WebSocket.
3. **Client** — `flow run --server http://desktop:8765` on the laptop: audio capture and typing stay local, inference happens on the server.

Server setup, the API, and the security model are in [docs/SERVER.md](docs/SERVER.md).

## Requirements

- A GPU is recommended: the defaults (Whisper `large-v3-turbo` plus a 4B polish model) fit comfortably in about 8 GB of VRAM.
- CPU-only works too: Flow auto-selects the `small` Whisper model on CPU. Polish on CPU is slower; disable it with `polish.enabled = false` if the latency annoys you.
- Python 3.10+ for pip/pipx installs. The release binaries bundle everything.

## Configuration

`flow config init` writes a documented `config.toml`; `flow config path` tells you where it lives. A taste:

```toml
[hotkey]
keys = ["KEY_F9"]           # any key you like
mode = "toggle"             # tap to start, tap to stop

[audio]
auto_stop_silence_s = 2.0   # hands-free: stop after 2s of silence

[replacements]
"vs code" = "VS Code"
```

Every option, every default, the `FLOW_*` environment variables, and the dictionary format: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Privacy

Flow has no accounts, no telemetry, and no cloud component. In local mode nothing touches the network except your own Ollama instance on localhost. In client/server mode audio travels only between your client and your server, with optional bearer-token auth — both ends are machines you own. Dictation history is off by default; if you turn it on, it's a local file you can delete.

## Accessibility

Flow exists because of a chronic illness, and it's built for limited energy and mobility first: toggle mode plus silence auto-stop means dictating without holding anything down, every hotkey and threshold is configurable, audio cues confirm state without needing to look, and the setup scripts try hard to leave nothing manual. If something about Flow is hard to use with your body, that's a bug — [please open an issue](https://github.com/lucyfromnaarm/flow-dictation/issues).

## Docs

- [Configuration reference](docs/CONFIGURATION.md)
- [Running a server](docs/SERVER.md)
- [Platform setup (Linux / macOS / Windows)](docs/PLATFORMS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## Contributing

Bug reports, backends for new compositors, and accessibility feedback are all welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) — `SPEC.md` is the architecture contract, and the test suite runs fully headless.

## License

[MIT](LICENSE).
