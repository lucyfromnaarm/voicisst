# Troubleshooting

Start here:

```bash
flow selftest                                # local setup
flow selftest --server http://big-box:8765   # remote setup
```

It checks your config, hotkey backend, a 1-second audio capture, engine
health (model load locally, `/v1/health` remotely), the typing backend, the
clipboard, and a polish round-trip, printing PASS/FAIL/SKIP per step.

Flow's own error messages usually contain the fix; the tables below collect
them plus the quieter failure modes.

## Recording and audio

| Symptom | Fix |
|---|---|
| "mic muted? rms=0.000001 — check pavucontrol / pipewire" | The mic is delivering silence. Unmute it / raise input gain (pavucontrol or your OS sound settings), and check the right device is default — or set `audio.input_device` by name or index. |
| Press, speak, release — nothing happens, no error | Recordings shorter than `audio.min_record_ms` (default 1000 ms) are dropped as accidental taps, and audio below `audio.rms_gate` (default 0.005) is dropped as silence. Hold longer, speak closer, or lower the thresholds. |
| Recording cuts off at 2 minutes | That's the `audio.max_record_ms` cap (default 120000). Raise it if you dictate longer monologues. |
| Recording stops while I pause to think | `audio.auto_stop_silence_s` is set; raise it or set it to `0.0` to disable auto-stop. |
| Whispered speech transcribes badly | Keep `audio.normalize = true` (the default) — it boosts quiet speech before transcription. |
| `flow selftest`: "got 0 samples from sounddevice" | No usable input device. Plug in / select a mic; on Linux make sure PortAudio is installed (`scripts/setup-linux.sh` does this). |

## Polish (Ollama / LLM)

On any polish failure Flow falls back to the raw transcript and shows the
reason — dictation keeps working, just unpolished.

| Symptom | Fix |
|---|---|
| "ollama not running — try `systemctl status ollama`" | Start it: `systemctl start ollama`, or run `ollama serve` by hand. Check `polish.url` (default `http://localhost:11434`). |
| "model not pulled — `ollama pull qwen3.5:4b`" | Pull the model named in `polish.model`: `ollama pull qwen3.5:4b`. |
| "ollama timeout — model may be cold" | The first request after idle loads the model into VRAM, which can exceed the timeout on slow disks/GPUs. Raise `polish.timeout` (or `FLOW_POLISH_TIMEOUT`); raise `polish.keep_alive` to stay loaded longer. |
| Polish is slow on short phrases | Thinking mode adds latency. Flow already skips it below `polish.think_min_chars` (100); lower that further or set `polish.think = false`. |
| Polished text changed something it shouldn't | Press Backspace during the processing window (streaming mode) to cancel polish and keep the raw transcript. Persistent miscorrections of names belong in the dictionary; mechanical fixes in `[replacements]`. |
| I just want raw Whisper output | `polish.enabled = false` (or `FLOW_POLISH_ENABLED=0`). |
| Games/training runs fight Ollama for VRAM | Set `polish.vram_unload_below_mb` (e.g. `1024`): a watchdog unloads the polish model when free VRAM drops below the threshold and reloads it on the next dictation. |

## Typing and pasting (Linux)

| Symptom | Fix |
|---|---|
| "no ydotool socket found ... start ydotoold (systemctl --user start ydotoold)" | `systemctl --user start ydotoold`. The socket should be at `$XDG_RUNTIME_DIR/.ydotool_socket` (override with `YDOTOOL_SOCKET`). A *system* ydotoold owns its socket as root — disable it and use the user unit that `scripts/setup-linux.sh` installs. |
| "ydotool not installed" | Install the `ydotool` package, or run `scripts/setup-linux.sh`. |
| "paste failed — text is on clipboard — press Ctrl+V" | Injection failed after copying, so paste manually; then check `flow selftest` for the ydotool/ydotoold state. |
| "terminal detected — text copied, press Ctrl+Shift+V to paste" | Not an error. In terminals Ctrl+V is a control character, so Flow copies the text and lets you paste with the terminal chord, Ctrl+Shift+V. Tune which windows count via `output.terminal_classes`. |
| Dictating into a terminal pastes garbage / triggers a plain paste (GNOME Wayland) | GNOME exposes no focused-window API, so terminal detection needs a Shell extension ("Window Calls"-style). Install one, or set `output.paste_chord = "ctrl-shift-v"`. See [PLATFORMS.md](PLATFORMS.md). |
| Each newline sends the message in Slack/Discord/Claude | Keep `output.newline_mode = "shift-enter"` (the default). Set `"enter"` only for apps that want real Enter. |
| Typed text has wrong characters (type mode) | Per-keystroke typing can fight unusual layouts. Use `output.mode = "paste"` (the default). |
| "wl-clipboard not installed" | Install `wl-clipboard` (Wayland) or `xclip` (X11). |

## Hotkeys

| Symptom | Fix |
|---|---|
| "no input device with hotkey ... are you in 'input' group?" | `groups \| grep input`; if missing: `sudo usermod -aG input $USER`, then **log out and back in**. `scripts/setup-linux.sh` does this for you. |
| "warning: unknown key name ..." | Key names must match the listening backend: evdev wants `KEY_*` names (`KEY_F9`), pynput wants names like `f9`, `alt_r`, `ctrl_r`. The pynput backend also accepts `KEY_*` names. See [CONFIGURATION.md](CONFIGURATION.md#hotkey-key-names). |
| Hotkey fires repeatedly while held | It shouldn't — both backends ignore key autorepeat. Update and report a bug if you still see it. |
| New keyboard not picked up / dictation dies when a keyboard disconnects | Flow drops dead devices and keeps running on the rest; restart Flow after plugging in a new keyboard. |
| Toggle mode: forgot to tap stop | Set `audio.auto_stop_silence_s` so recording ends itself, and remember the 2-minute `max_record_ms` cap backstops it. |

## macOS

| Symptom | Fix |
|---|---|
| Transcription works but nothing appears in the app | Grant **Accessibility**: System Settings → Privacy & Security → Accessibility → add your terminal app or the Flow binary. Restart Flow after granting. |
| The hotkey never triggers | Grant **Input Monitoring** (same Settings pane) to the app that launches Flow. |
| "got 0 samples" / silent recordings | Grant **Microphone** permission to the launching app. |
| Granted permissions but still nothing | Permissions attach to the app that *launches* Flow — Terminal.app and iTerm2 are separate grants. Re-add the one you actually use, restart Flow. |

## Windows

| Symptom | Fix |
|---|---|
| Antivirus quarantines or blocks Flow | The global hotkey hook trips keylogger heuristics. Add an exclusion for the Flow executable — and read `src/flow_dictation/hotkeys/pynput_listener.py` if you want to verify what it does. |
| Paste lands in the wrong window | Click the target window before releasing the hotkey; injection goes to whatever has focus at delivery time. |

## Remote mode

| Symptom | Fix |
|---|---|
| "is `flow serve` running on http://...?" | Check the server process, the URL/port in `engine.server_url`, and the server box's firewall. Probe it: `curl http://big-box:8765/v1/health`. |
| 401 Unauthorized | The client's `engine.token` doesn't match the server's `server.token`. |
| Server reachable from the box itself but not the LAN | The server binds `127.0.0.1` by default; start it with `--host 0.0.0.0` — and set a token. |
| Requests time out on long recordings | Raise the client's `engine.request_timeout` (default 120 s) and/or the server's polish timeout. |
| Streaming partials lag | The server re-transcribes the whole buffer on its own cadence; a big model on a slow device falls behind on long dictations. Use a faster Whisper model or a GPU on the server. |

## Everything else

| Symptom | Fix |
|---|---|
| "tray extra not installed" | `pip install "flow-dictation[tray]"`, then `flow run --tray` or `ui.tray = true`. |
| GPU sitting idle, transcription slow | `whisper.device = "auto"` falls back to CPU when CUDA isn't usable. Force `whisper.device = "cuda"` to surface the real error — usually missing NVIDIA driver or cuBLAS/cuDNN libraries for faster-whisper. |
| Which config file am I editing? | `flow config path` prints it; `flow config show` prints the effective settings after env vars and flags. |
| Settings ignored | Check precedence: defaults < config.toml < legacy env vars < `FLOW_*` env vars < CLI flags. A stray `FLOW_*` variable in your shell profile beats the file. |
| Where did my dictation go? | Delivery falls back: paste → clipboard + notification → stderr. Check the terminal/journal running Flow; enable `[history]` to keep a local JSONL log. |
