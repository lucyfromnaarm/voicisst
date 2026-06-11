# Voicisst — Architecture Specification

Voicisst is a free, open-source voice-dictation app: speak into any app on any
OS, get clear, polished writing. This document is the **contract** between
modules. Implementations must match the signatures and semantics here.

## Product requirements

1. **Works in every app** — types/pastes into whatever has focus.
2. **Polished output** — an LLM reworks raw speech: removes fillers
   ("um", "uh"), applies self-corrections ("at 2… actually 3" → "at 3"),
   formats spoken lists into numbered Markdown lists, punctuates.
3. **100+ languages** — Whisper auto-detect; polish responds in the input
   language.
4. **Spells names right** — user dictionary + (Linux) primary-selection
   context fed to both Whisper (initial_prompt) and the polisher.
5. **Whisper-quiet dictation** — RMS normalization boosts quiet speech.
6. **Accessible by design** — hold-to-talk *and* toggle modes, silence
   auto-stop, audio cues, all hotkeys configurable. Built for users with
   limited energy/mobility (ME/CFS); minimal setup friction.
7. **Three run modes**:
   - **All-in-one** (`voicisst run`): capture + transcribe + polish + inject,
     all local.
   - **Server** (`voicisst serve`): exposes transcription/polish over HTTP+WS
     (runs on the big-GPU box).
   - **Client** (`voicisst run --server URL`): capture + inject locally,
     inference remote.
8. **Cross-platform**: Linux (Wayland + X11), macOS, Windows.

## Repository layout

```
pyproject.toml              dist: voicisst, CLI: voicisst
src/voicisst/
  __init__.py               __version__
  config.py                 TOML + env + CLI config (COMPLETE — do not edit)
  textproc.py               sanitize/strip_think/replacements (COMPLETE)
  audio.py                  Recorder, beeps, rms, normalize, resample, SilenceDetector
  notify.py                 stderr + best-effort desktop notifications
  transcribe.py             faster-whisper wrapper
  polish.py                 prompt + Ollama/OpenAI-compat polishers + VRAM watchdog
  clipboard.py              cross-platform copy/paste-selection helpers
  inject/
    base.py                 Injector ABC (COMPLETE — do not edit)
    __init__.py             get_injector() picker
    ydotool.py xdotool.py pynput_injector.py windowinfo.py
  hotkeys/
    base.py                 HotkeyListener ABC (COMPLETE — do not edit)
    __init__.py             get_listener() picker
    evdev_listener.py pynput_listener.py
  engine/
    base.py                 Engine + StreamSession ABCs (COMPLETE — do not edit)
    __init__.py             get_engine() factory
    local.py remote.py
  server/
    __init__.py app.py      FastAPI app factory + uvicorn entry
  protocol.py               WAV codec + message schema constants
  streaming.py              StreamingTyper (live typing w/ diff replace)
  dictation.py              DictationApp orchestrator
  cli.py                    click CLI (voicisst run/serve/selftest/config)
  selftest.py               environment diagnostics
  tray.py                   optional pystray tray icon
tests/                      pytest; NO hardware/network/GPU at test time
.github/workflows/          ci.yml (lint+test matrix), release.yml (binaries)
packaging/                  pyinstaller spec, systemd units, launchd plist, etc.
scripts/                    install.sh, install.ps1, setup-linux.sh
docs/                       configuration, server, platforms, troubleshooting
```

## Hard rules (all modules)

- **Lazy imports** for anything hardware/platform/heavy: `sounddevice`,
  `evdev`, `pynput`, `faster_whisper`, `fastapi`, `pystray`. Import inside
  functions/methods, never at module top. `numpy`, `requests`, stdlib are
  fine at top level.
- Tests must pass headless on CI (no audio device, no GPU, no DISPLAY, no
  network). Mock `subprocess`, `requests`, and hardware modules
  (`monkeypatch.setitem(sys.modules, "faster_whisper", fake)`).
- Python ≥ 3.10. Type hints everywhere. `from __future__ import annotations`.
- Ruff-clean: line length 100, rules E,F,W,I,UP,B.
- Errors at runtime must be *helpful*: catch, explain the likely fix
  (e.g. "ollama not running — try `systemctl status ollama`").
- All user-facing strings/output delivered to the focused window MUST pass
  through `textproc.sanitize()` first (terminal-escape safety).
- No print() to stdout in library code; use `notify.py` helpers or stderr.

## config.py (provided — read it, do not modify)

`Config` dataclass with sections: `engine, whisper, polish, hotkey, audio,
output, dictionary, replacements, server, ui, history`. See the file for
every field and default. Key API:

```python
cfg = load_config(path=None, env=os.environ, overrides={"whisper.model": "small"})
cfg.whisper.resolved_model(device)   # "auto" -> "large-v3-turbo" (cuda) / "small" (cpu)
config_path() -> Path                # platformdirs user config dir
data_dir() -> Path
default_config_toml() -> str         # documented template for `voicisst config init`
```

## engine/base.py (provided)

```python
class EngineError(RuntimeError): ...   # .hint: str — user-facing fix suggestion

class StreamSession(ABC):
    def feed(self, chunk: np.ndarray) -> None          # float32 mono append
    def partial(self) -> str | None                    # latest full raw transcript, None if unchanged
    def finalize(self, *, vocab: str = "") -> Iterator[str]
        # yields successive FULL-TEXT polished snapshots; last yield is final.
        # If polish disabled/fails: yields raw transcript once.
    def cancel(self) -> None
    def close(self) -> None

class Engine(ABC):
    def transcribe(self, audio: np.ndarray, sample_rate: int, *,
                   language: str | None = None, vocab: str = "") -> str
    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str
    def polish_stream(self, text, *, language=None, vocab="") -> Iterator[str]
        # full-text snapshots, last is final; on failure yield original once
    def open_stream(self, sample_rate: int, *, language: str | None = None,
                    vocab: str = "") -> StreamSession | None   # None = unsupported
    def health(self) -> dict
    def warm(self) -> None        # default no-op
    def close(self) -> None       # default no-op
```

`language=None` means auto-detect. `audio` is always float32 mono ndarray.

`engine/__init__.py` exports `get_engine(cfg) -> Engine`:
mode "local" → `LocalEngine(cfg)`; "remote" → `RemoteEngine(cfg)`. A
`--server URL` CLI flag sets mode=remote + url via config overrides.

### LocalEngine (engine/local.py)
Wraps `transcribe.Transcriber` + `polish.get_polisher(cfg)`. Lazy-builds on
first use; `warm()` preloads both. `open_stream` returns a session that
accumulates fed chunks and re-transcribes the **entire buffer** in
`partial()` (call-throttled by the caller's tick; use a non-blocking lock —
if a transcription is already running, return None). `health()` returns
`{"status","version","mode":"local","whisper_model","device","polish_backend","polish_model"}`.

### RemoteEngine (engine/remote.py)
HTTP via `requests.Session` with `Authorization: Bearer <token>` when
token set. Endpoints below. `open_stream` uses `websocket-client` (sync).
Connection errors → `EngineError` with hint ("is `voicisst serve` running on
<url>?"). `polish_stream` may yield once (single REST call) — that's fine.

## HTTP/WS protocol (protocol.py + server/app.py)

Audio over the wire: **WAV bytes, 16-bit PCM mono**, base64 in JSON.
`protocol.py` provides:

```python
encode_wav(audio: np.ndarray, sample_rate: int) -> bytes      # float32 -> int16 WAV
decode_wav(data: bytes) -> tuple[np.ndarray, int]             # -> float32 [-1,1], sr
PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765
```

REST (all JSON; auth via Bearer when server token set; 401 otherwise):

```
GET  /v1/health          -> {"status":"ok","version",..., engine health fields}
POST /v1/transcribe      {"audio_b64", "language": null|code, "vocab": ""}
                         -> {"text"}
POST /v1/polish          {"text", "language": null|code, "vocab": ""} -> {"text"}
POST /v1/process         {"audio_b64","language","vocab","polish":true}
                         -> {"raw","text"}
```

WS `/v1/stream?token=...`:
- client → text frame `{"type":"start","sample_rate":16000,"language":null,"vocab":""}`
- client → binary frames: raw **int16 PCM** chunks (no WAV header)
- server → `{"type":"partial","text": "..."}` (full raw transcript so far,
  sent whenever it changes; server re-transcribes on its own cadence)
- client → `{"type":"finalize","vocab":""}` →
  server → zero+ `{"type":"polish","text"}` full-text snapshots, then
  `{"type":"final","text","raw"}` and closes.
- client → `{"type":"cancel"}` → server closes.
- errors: `{"type":"error","message","hint"}`.

`server/app.py`:
```python
def create_app(engine: Engine, *, token: str = "") -> "FastAPI"   # engine injected (tests pass a fake)
def serve(cfg: Config) -> None    # builds LocalEngine, warms, uvicorn.run
```
Server must run blocking engine calls in a threadpool (`run_in_executor` /
`fastapi.concurrency.run_in_threadpool`). Warn loudly at startup if host is
non-loopback and token is empty.

## transcribe.py

```python
class Transcriber:
    def __init__(self, cfg: WhisperConfig): ...       # picks device/compute; loads model (faster-whisper only)
    def transcribe(self, audio, sample_rate=16000, *, language=None, vocab="") -> str
    device: str; model_name: str
```
Resample to 16 kHz if needed (use `audio.resample`). `language=None` →
auto-detect. `vocab` → `initial_prompt`. beam_size/vad from cfg.

## polish.py

Port the prototype's `POLISH_SYSTEM_PROMPT` **verbatim including all
examples**, then append:
- a multilingual rule: "Always respond in the same language as the input.
  The rules above apply in every language."
- a dictionary hook: when vocab words present, append
  "Preferred spellings (use these exactly): <words>".

```python
def build_system_prompt(vocab: str = "") -> str
class Polisher(ABC):  # in polish.py, small
    def polish(self, text, *, language=None, vocab="") -> str
    def polish_stream(self, text, *, language=None, vocab="") -> Iterator[str]  # full-text snapshots
    def warm(self) -> None
    def unload(self) -> None     # best-effort free VRAM
class OllamaPolisher(Polisher)   # port prototype incl. think-mode hybrid, /no_think, <think> stripping
class OpenAICompatPolisher(Polisher)  # POST {url}/v1/chat/completions, stream=True SSE; api_key optional
class VramWatchdog               # port OllamaKeepalive; enabled when cfg.polish.vram_unload_below_mb > 0
def get_polisher(cfg) -> Polisher | None   # None when backend=="none" or not enabled
```
On any polish failure: return/yield the input text unchanged + notify hint.
Use `textproc.strip_think` / `strip_quotes`.

## audio.py

```python
class Recorder:                  # port from prototype; lazy sounddevice
    def __init__(self, samplerate=16000, device: str | int | None = None)
    def start(self); def stop(self) -> tuple[np.ndarray, float]  # (audio, duration_ms)
    def is_active(self) -> bool; def elapsed_ms(self) -> float
    chunks: list[np.ndarray]     # live-readable by StreamSession feeders
def rms(audio: np.ndarray) -> float
def normalize(audio, target_rms=0.05, max_gain=30.0) -> np.ndarray  # whisper-quiet boost; no-op if audio empty/silent
def resample(audio, sr_from: int, sr_to: int) -> np.ndarray         # linear interp, no scipy
class SilenceDetector:           # for auto-stop: feed(chunk)->None, .triggered: bool
    def __init__(self, silence_s: float, rms_gate: float, sample_rate: int)
def play_beep(kind: str, enabled: bool = True) -> None
    # kinds: start/stop/cancel/error; synth sine in-memory; play via
    # sounddevice (sd.play, non-blocking); swallow all errors headlessly
```

## notify.py

```python
def notify(summary: str, body: str = "", urgency: str = "low", *, enabled: bool = True) -> None
```
Always logs to stderr. If enabled, best-effort desktop notification:
Linux `notify-send`, macOS `osascript -e 'display notification ...'`
(escape quotes!), Windows powershell toast — all via subprocess, short
timeout, never raise.

## clipboard.py

```python
def copy(text: str) -> bool          # wl-copy → xclip → pbcopy → win (powershell Set-Clipboard or ctypes)
def read_primary_selection() -> str  # Linux wl-paste --primary / xclip -o -sel primary; "" elsewhere
```

## inject/ (base.py provided)

```python
class Injector(ABC):
    name: str
    @classmethod def available(cls) -> bool
    def type_text(self, text: str) -> bool     # translate \n per cfg.output.newline_mode
    def backspace(self, n: int) -> bool        # exactly n, never more
    def paste_chord(self) -> bool              # press the paste shortcut (Ctrl+V / Cmd+V / Ctrl+Shift+V variant NOT here — plain paste)
    def tap_escape(self) -> bool
```
- `ydotool.py`: port all prototype ydotool code (timing-flag detection,
  shift-enter newlines, batched backspace).
- `xdotool.py`: X11 fallback (`xdotool type --delay`, `key BackSpace`, etc.).
- `pynput_injector.py`: macOS/Windows (and X11 fallback): keyboard
  Controller .type(), Key.backspace taps, cmd/ctrl+v chord.
- `windowinfo.py`: `focused_window_class() -> str | None` (port GNOME/
  Sway/Hyprland detection; macOS via osascript frontmost app name;
  Windows via ctypes GetForegroundWindow — all best-effort) and
  `looks_like_terminal(cls, terminal_classes) -> bool`.
- `__init__.py`: `get_injector(cfg) -> Injector` — Linux: ydotool → xdotool
  → pynput; macOS/Windows: pynput. Raise EngineError-style helpful error if
  none available.

## hotkeys/ (base.py provided)

```python
Callbacks = on_press: Callable[[], None], on_release, on_backspace
class HotkeyListener(ABC):
    def __init__(self, keys: Sequence[str], on_press, on_release, on_backspace=None)
    def start(self) -> None    # non-blocking (thread)
    def stop(self) -> None
    @classmethod def available(cls) -> bool
```
- `evdev_listener.py`: port prototype loop (multi-device select, holder-fd
  arbitration, autorepeat ignore, dead-device removal, skip ydotool virtual
  kbd, backspace→on_backspace). Key names are evdev names ("KEY_MENU").
- `pynput_listener.py`: pynput global listener; key names are pynput names
  ("alt_r", "ctrl_r", "f9", single chars). Map both; tolerate unknown names
  with a warning. Suppress autorepeat (track held state).
- `__init__.py`: `get_listener(cfg, callbacks) -> HotkeyListener` —
  backend "auto": Linux tries evdev (permission check!) then pynput;
  mac/win → pynput. Helpful errors (input group hint on Linux).

## streaming.py

Port `StreamingTyper` generalized over `Injector` + `Engine.StreamSession`:

```python
class StreamingTyper:
    def __init__(self, session: StreamSession, injector: Injector, tick_s: float)
    def start(self); def stop(self) -> str      # returns last typed text
    def replace_with(self, target: str) -> None # diff-aware (common prefix), exact backspace count
    def erase_all(self) -> None
    def set_suffix(self, suffix: str) -> None   # swap trailing status suffix
```
Invariant preserved from prototype: `last_typed` always mirrors what's on
screen; never backspace more than typed. The tick loop calls
`session.partial()` and `replace_with(raw)` on change.

## dictation.py

```python
class DictationApp:
    def __init__(self, cfg: Config, engine: Engine, injector: Injector | None = None)
    def run(self) -> None        # blocking; builds listener, worker thread, event queue
    def stop(self) -> None
```
Behavior (port prototype worker_loop + run_loop, generalized):
- hotkey mode "hold": press=start, release=stop. "toggle": press toggles;
  release ignored. Backspace during polish window → cancel polish (raw text).
- min/max record, muted-RMS check, RMS gate, normalization
  (cfg.audio.normalize) before transcribe.
- vocab = dictionary words (cfg.dictionary.words + file at
  cfg.dictionary.path, one term/line, '#' comments) + primary selection
  when cfg.dictionary.use_selection (Linux). Selection capture at press
  time, like prototype.
- silence auto-stop when cfg.audio.auto_stop_silence_s > 0 (works in both
  modes; essential for toggle/hands-free).
- streaming path (cfg.output.stream and engine.open_stream not None):
  StreamingTyper with live partials, then "[Processing…]" suffix, then
  polished snapshots streamed in via replace_with. Non-streaming path:
  transcribe → polish → deliver.
- deliver(): sanitize → replacements → terminal? copy+notify : paste
  (copy + injector.paste_chord) or type per cfg.output.mode; fallbacks and
  notify hints exactly like prototype.
- history: when enabled, append {"ts","raw","text","app"} JSONL to
  cfg.history.path.
- beeps + notifications at the same points as prototype.

## cli.py

click group `voicisst`; invoking bare `voicisst` == `voicisst run`.
- `voicisst run [--server URL] [--token T] [--stream/--no-stream] [--toggle]
  [--language L] [--config PATH] [--tray]`
- `voicisst serve [--host H] [--port P] [--token T] [--config PATH]`
- `voicisst selftest [--server URL]`
- `voicisst config init|show|path`
- `voicisst version`
`main()` is the console entry point. Map CLI flags to config overrides.
Catch EngineError → friendly message + exit 1, no traceback.

## selftest.py

Port prototype selftest, generalized: config summary, hotkey backend check,
audio capture (1s), engine health (local: model load; remote: GET health),
injector availability, clipboard, polish round-trip (skipped if disabled).
Steps print PASS/FAIL/SKIP; exit code 0/1. Must degrade gracefully headless.

## tray.py

`run_tray(app: DictationApp, cfg) -> None` — optional pystray icon
(generated PIL circle), menu: status, toggle polish, quit. ImportError →
notify "tray extra not installed: pip install voicisst[tray]".

## Packaging & CI

- `ci.yml`: push/PR → ruff check + pytest on {ubuntu, macos, windows} ×
  py{3.10, 3.12}. Ubuntu needs `sudo apt-get install -y libportaudio2`.
  Install `-e .[server,dev]` (NOT local/faster-whisper — tests mock it).
- `release.yml`: on tag `v*` → (1) build sdist+wheel (`python -m build`),
  (2) PyInstaller onedir per OS (ubuntu-22.04, macos-13 x86_64, macos-14
  arm64, windows-latest) bundling `[local,server]`, zipped as
  `voicisst-<version>-<os>-<arch>.{tar.gz,zip}`, (3) GitHub Release with all
  artifacts + checksums, (4) optional PyPI publish job gated on
  `PYPI_API_TOKEN` secret existing.
- `packaging/pyinstaller/voicisst.spec`: entry `voicisst.cli:main`,
  collect ctranslate2/faster_whisper data, sounddevice's portaudio binary.
- `scripts/install.sh`: pipx/uv-based install for Linux/macOS + offer
  `setup-linux.sh` (port of setup.sh: multi-distro dnf/apt/pacman/zypper,
  ydotool, udev uinput rule, input group, user systemd units).
- `scripts/install.ps1`: Windows pipx/uv install.
- `packaging/systemd/`: voicisst.service + ydotoold.service (user units,
  paths via `%h/.local/bin/voicisst run`).
- `packaging/macos/one.octavia.voicisst.plist`: LaunchAgent template.

## Testing conventions

- `tests/conftest.py` may add shared fixtures (tmp config, fake engine).
- Use `monkeypatch` for subprocess/requests/sys.modules. No `time.sleep`
  > 0.2s in tests. Mark nothing as skip-on-CI; everything must run.
- FastAPI tests: `fastapi.testclient.TestClient` with a `FakeEngine`
  (deterministic strings), including a WS round-trip test.
