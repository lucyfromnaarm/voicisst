# Configuration

Voicisst reads a TOML file, environment variables, and CLI flags, in this order
(later wins):

1. Built-in defaults
2. `config.toml`
3. Legacy environment variables (`WHISPER_MODEL`, `OLLAMA_MODEL`, ... — kept
   from the original prototype)
4. `VOICISST_*` environment variables
5. CLI flags (`--server`, `--toggle`, `--language`, ...)

Useful commands:

```bash
voicisst config path   # where the config file lives on this machine
voicisst config init   # write a fully documented template there
voicisst config show   # print the effective config after all overrides
```

Typical locations: `~/.config/voicisst/config.toml` on Linux,
`~/Library/Application Support/voicisst/config.toml` on macOS, under
`%LOCALAPPDATA%` on Windows. `voicisst config path` is authoritative.

Every setting is optional. The values below are the defaults.

## [engine]

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"local"` | `"local"` runs Whisper and the polisher on this machine. `"remote"` sends audio to a `voicisst serve` instance. |
| `server_url` | `""` | Server base URL for remote mode, e.g. `"http://big-box:8765"`. `voicisst run --server URL` sets both this and `mode = "remote"`. |
| `token` | `""` | Bearer token for the remote server. Must match the server's `server.token`. |
| `request_timeout` | `120.0` | Timeout in seconds for HTTP requests to the server. |

## [whisper]

| Key | Default | Meaning |
|---|---|---|
| `model` | `"auto"` | Any faster-whisper model name (`"large-v3-turbo"`, `"small"`, `"base.en"`, ...). `"auto"` resolves to `large-v3-turbo` on CUDA and `small` on CPU. |
| `device` | `"auto"` | `"auto"` \| `"cuda"` \| `"cpu"`. Auto picks CUDA when a usable GPU is found. |
| `compute` | `""` | faster-whisper `compute_type` override. Empty picks a sensible default for the device (`float16` on CUDA, `int8` on CPU). |
| `language` | `"auto"` | `"auto"` detects the spoken language (100+ supported); or pin an ISO code: `"en"`, `"es"`, `"zh"`, ... |
| `beam_size` | `5` | Whisper beam search width. |
| `vad_filter` | `false` | Enable faster-whisper's built-in voice-activity filter. |

## [polish]

The polish step sends the raw transcript to an LLM that removes fillers,
applies self-corrections, formats lists, and punctuates. If polish fails for
any reason, Voicisst delivers the raw transcript instead and tells you why.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Turn the LLM cleanup on/off. |
| `backend` | `"ollama"` | `"ollama"` \| `"openai"` (any OpenAI-compatible `/v1/chat/completions` server: llama.cpp, vLLM, LM Studio, ...) \| `"none"`. |
| `model` | `"qwen3.5:4b"` | Model name. For Ollama, pull it first: `ollama pull qwen3.5:4b`. |
| `url` | `"http://localhost:11434"` | Base URL of the Ollama or OpenAI-compatible server. |
| `api_key` | `""` | API key, for OpenAI-compatible backends that want one. |
| `keep_alive` | `"30m"` | How long Ollama keeps the model loaded after a request. |
| `num_ctx` | `8192` | Context window passed to the backend. |
| `num_predict` | `2048` | Max tokens the polisher may generate. |
| `think` | `false` | Allow "thinking" models to reason before answering. Off by default: thinking adds many seconds of latency per utterance, and the polish task rarely needs it. Opt in for long-form quality if you like. |
| `think_min_chars` | `100` | Even with `think = true`, skip thinking for inputs shorter than this — short utterances don't benefit. |
| `num_gpu` | `-1` | Ollama `num_gpu` option (layers to offload). `-1` lets the backend decide. |
| `timeout` | `60.0` | Per-request timeout in seconds. Raise it if your model is slow to cold-start. |
| `vram_unload_below_mb` | `0` | When `> 0`, a watchdog polls `nvidia-smi` every 60s and unloads the polish model from Ollama if free VRAM drops below this many MB — so a game or training run can have the memory. The model reloads on the next dictation. `0` disables. |

### How think mode is negotiated

Ollama models differ in how thinking is switched off, so the Ollama backend
negotiates it on the first request:

- Ollama-native thinking models (qwen3.5, deepseek-r1, ...) honor the
  `think` API field. Voicisst always sends it — without `think: false` these
  models burn the whole `num_predict` budget on a separate thinking channel
  and return an *empty* response.
- Models pulled straight from Hugging Face as GGUFs have no thinking
  template and reject the `think` field with a 400. Voicisst detects that once,
  then falls back to appending a `/no_think` marker to the prompt instead.

Either way, any `<think>...</think>` blocks that leak into the output are
stripped before the text reaches your screen.

## [hotkey]

| Key | Default | Meaning |
|---|---|---|
| `keys` | Linux: `["KEY_COMPOSE", "KEY_MENU"]`; macOS: `["alt_r"]`; Windows: `["ctrl_r"]` | The dictation key(s). Any of them triggers. See [key name formats](#hotkey-key-names) below. |
| `mode` | `"hold"` | `"hold"` = push-to-talk (press to record, release to stop). `"toggle"` = tap to start, tap again to stop. |
| `backend` | `"auto"` | `"auto"` \| `"evdev"` \| `"pynput"`. Auto tries evdev on Linux (needs `input` group membership), falling back to pynput; macOS/Windows always use pynput. |

### Hotkey key names

Which names are valid depends on the backend that ends up listening:

- **evdev** (Linux): kernel key names like `KEY_MENU`, `KEY_COMPOSE`,
  `KEY_RIGHTALT`, `KEY_RIGHTCTRL`, `KEY_F9`, `KEY_CAPSLOCK`. The full list is
  in `/usr/include/linux/input-event-codes.h`, or run `python -m evdev.evtest`
  and press the key you want.
- **pynput** (macOS, Windows, Linux fallback): pynput names like `alt_r`,
  `ctrl_r`, `cmd_r`, `shift_r`, `f9`, `menu`, or a single character like
  `"a"`.

The pynput backend also understands the evdev-style `KEY_*` names, so a
config written on Linux keeps working if Voicisst falls back to pynput. Unknown
names produce a warning rather than an error.

## [audio]

| Key | Default | Meaning |
|---|---|---|
| `sample_rate` | `16000` | Capture sample rate in Hz. |
| `input_device` | `""` | Microphone by name or index; `""` = system default. |
| `min_record_ms` | `1000` | Recordings shorter than this are dropped silently (filters accidental taps). |
| `max_record_ms` | `120000` | Hard cap; recording auto-stops after 2 minutes. |
| `muted_rms` | `0.00001` | Audio quieter than this triggers the "mic muted?" warning and is dropped. |
| `rms_gate` | `0.005` | Audio quieter than this (but above `muted_rms`) is treated as silence and dropped without a warning. |
| `auto_stop_silence_s` | `0.0` | When `> 0`, recording stops automatically after this many seconds of trailing silence. Works in both hold and toggle mode; this is the hands-free option. |
| `normalize` | `true` | Boost quiet/whispered speech to a usable level before transcription. |

## [output]

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"paste"` | `"paste"` copies the text and sends the paste shortcut (fast, layout-proof). `"type"` sends individual keystrokes. |
| `stream` | `false` | Live-type the raw transcript while you speak, then replace it with the polished text. Pressing Backspace during the polish window cancels polish and keeps the raw transcript. |
| `stream_tick_ms` | `600` | How often streaming mode re-transcribes the audio so far. |
| `key_delay_ms` | `0` | Inter-key delay for the typing backend (ydotool `--key-delay`, xdotool `--delay`). |
| `key_hold_ms` | `0` | Key hold time (ydotool `--key-hold`); `0` = backend default. |
| `paste_chord` | `"auto"` | Paste shortcut: `"auto"` (Ctrl+V, or Cmd+V on macOS) \| `"ctrl-v"` \| `"ctrl-shift-v"` \| `"cmd-v"`. |
| `newline_mode` | `"shift-enter"` | How `\n` is typed: `"shift-enter"` keeps chat apps (Slack, Discord, Claude) from treating each newline as "send". `"enter"` sends plain Enter. |
| `terminal_classes` | `["kitty", "alacritty", "foot", "wezterm", "konsole", "org.gnome.Terminal", "xterm", "ptyxis", "terminal", "iterm2", "windowsterminal", "cmd.exe", "powershell"]` | Window classes treated as terminals. In a terminal, Voicisst copies the text to the clipboard and notifies you to press Ctrl+Shift+V instead of injecting a paste. |

## [dictionary]

Names and jargon Voicisst should spell correctly. Dictionary words are fed to
Whisper as the `initial_prompt` and to the polisher as preferred spellings.

| Key | Default | Meaning |
|---|---|---|
| `path` | `""` | Path to a dictionary file; `""` = `dictionary.txt` in the Voicisst data directory. |
| `words` | `[]` | Inline list of terms, merged with the file. |
| `use_selection` | `true` | Linux: text highlighted at the moment you press the hotkey (the primary selection) is added as spelling context for that one dictation. |

### Dictionary file format

One term per line; `#` starts a comment:

```
# people
Anja Šimunović
Toivo
# projects
voicisst
ctranslate2
```

## [replacements]

Find/replace pairs applied after polish: case-insensitive, whole-word, longer
patterns first (so `"vs code insiders"` wins over `"vs code"`). The
replacement's casing is used exactly as written.

```toml
[replacements]
"vs code" = "VS Code"
"github" = "GitHub"
"k eight s" = "k8s"
```

Replacements can only be set in the config file (or programmatic overrides) —
there is no environment-variable form.

## [server]

Settings for `voicisst serve`. See [SERVER.md](SERVER.md).

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address. Set `"0.0.0.0"` to serve your LAN — set a token if you do. |
| `port` | `8765` | TCP port. |
| `token` | `""` | When set, every request must carry `Authorization: Bearer <token>` (WebSocket: `?token=`); otherwise the server answers 401. |

## [ui]

| Key | Default | Meaning |
|---|---|---|
| `beep` | `true` | Short tones on start/stop/cancel/error. |
| `notify` | `true` | Desktop notifications (always also logged to stderr). |
| `tray` | `false` | Tray icon; needs the extra: `pip install "voicisst[tray]"`. |

## [history]

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Log every dictation locally. |
| `path` | `""` | JSONL file; `""` = `history.jsonl` in the Voicisst data directory. Each line: `{"ts", "raw", "text", "app"}`. |

## Environment variables

Any setting can be overridden with `VOICISST_<SECTION>_<KEY>`:

```bash
VOICISST_WHISPER_MODEL=small
VOICISST_POLISH_ENABLED=0
VOICISST_HOTKEY_KEYS="KEY_F9,KEY_MENU"          # lists are comma-separated
VOICISST_AUDIO_AUTO_STOP_SILENCE_S=2.0
VOICISST_ENGINE_SERVER_URL=http://big-box:8765
VOICISST_OUTPUT_PASTE_CHORD=ctrl-shift-v
```

Booleans: `0`, `false`, `no`, `off`, and the empty string mean false
(case-insensitive); anything else means true.

### Legacy variables

These pre-date the config file and still work (the `VOICISST_*` form wins when
both are set):

| Legacy variable | Maps to |
|---|---|
| `WHISPER_MODEL` | `whisper.model` |
| `WHISPER_DEVICE` | `whisper.device` |
| `WHISPER_COMPUTE` | `whisper.compute` |
| `OLLAMA_MODEL` | `polish.model` |
| `OLLAMA_URL` | `polish.url` |
| `POLISH_ENABLED` | `polish.enabled` |
| `POLISH_KEEP_ALIVE` | `polish.keep_alive` |
| `POLISH_NUM_CTX` | `polish.num_ctx` |
| `POLISH_NUM_PREDICT` | `polish.num_predict` |
| `POLISH_THINK` | `polish.think` |
| `POLISH_THINK_MIN_CHARS` | `polish.think_min_chars` |
| `POLISH_NUM_GPU` | `polish.num_gpu` |
| `POLISH_TIMEOUT` | `polish.timeout` |
| `VRAM_UNLOAD_BELOW_MB` | `polish.vram_unload_below_mb` |
| `HOTKEY_NAMES` | `hotkey.keys` |
| `MIN_RECORD_MS` | `audio.min_record_ms` |
| `MAX_RECORD_MS` | `audio.max_record_ms` |
| `MUTED_RMS` | `audio.muted_rms` |
| `RMS_GATE` | `audio.rms_gate` |
| `SAMPLE_RATE` | `audio.sample_rate` |
| `OUTPUT_MODE` | `output.mode` |
| `STREAM` | `output.stream` |
| `STREAM_TICK_MS` | `output.stream_tick_ms` |
| `KEY_DELAY_MS` | `output.key_delay_ms` |
| `KEY_HOLD_MS` | `output.key_hold_ms` |
| `NEWLINE_MODE` | `output.newline_mode` |
| `TERMINAL_CLASSES` | `output.terminal_classes` |
| `BEEP` | `ui.beep` |

`WHISPER_BACKEND` from the prototype is recognized but ignored — Voicisst uses
faster-whisper only.

## CLI overrides

CLI flags have the last word:

```bash
voicisst run --server http://big-box:8765 --token s3cret   # engine.mode=remote + url + token
voicisst run --toggle                                       # hotkey.mode=toggle
voicisst run --stream / --no-stream                         # output.stream
voicisst run --language de                                  # whisper.language
voicisst run --config /path/to/other.toml                   # alternate config file
voicisst run --tray                                         # ui.tray=true
voicisst serve --host 0.0.0.0 --port 8765 --token s3cret    # server.*
```
