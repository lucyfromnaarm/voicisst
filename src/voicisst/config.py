"""Configuration: TOML file + environment variables + programmatic overrides.

Precedence (lowest to highest): dataclass defaults -> config.toml ->
legacy env vars (WHISPER_MODEL etc.) -> VOICISST_* env vars -> `overrides`.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "voicisst"

_DEFAULT_TERMINALS = (
    "kitty,alacritty,foot,wezterm,konsole,org.gnome.Terminal,xterm,ptyxis,"
    "terminal,iterm2,windowsterminal,cmd.exe,powershell"
)


def _default_hotkeys() -> list[str]:
    if sys.platform == "linux":
        # evdev names; pynput fallback maps these too.
        return ["KEY_COMPOSE", "KEY_MENU"]
    if sys.platform == "darwin":
        return ["alt_r"]  # right Option
    return ["ctrl_r"]  # Windows: right Ctrl


@dataclass
class EngineConfig:
    mode: str = "local"  # "local" | "remote"
    server_url: str = ""  # e.g. "http://big-box:8765"
    token: str = ""  # bearer token for remote server
    request_timeout: float = 120.0


@dataclass
class WhisperConfig:
    model: str = "auto"  # "auto" | any faster-whisper model name
    device: str = "auto"  # "auto" | "cuda" | "cpu"
    compute: str = ""  # faster-whisper compute_type override
    language: str = "auto"  # "auto" | ISO code ("en", "es", "zh", ...)
    beam_size: int = 5
    vad_filter: bool = False

    def resolved_model(self, device: str) -> str:
        if self.model != "auto":
            return self.model
        return "large-v3-turbo" if device == "cuda" else "small"

    def language_or_none(self) -> str | None:
        lang = self.language.strip().lower()
        return None if lang in ("", "auto") else lang


@dataclass
class PolishConfig:
    enabled: bool = True
    backend: str = "ollama"  # "ollama" | "openai" | "none"
    model: str = "qwen3.5:4b"
    url: str = "http://localhost:11434"  # ollama or OpenAI-compatible base URL
    api_key: str = ""
    keep_alive: str = "30m"
    num_ctx: int = 8192
    num_predict: int = 2048
    # Thinking adds many seconds of latency per utterance; for dictation the
    # polish task rarely needs it. Opt in for long-form quality if you like.
    think: bool = False
    think_min_chars: int = 100  # even when enabled, skip thinking below this length
    num_gpu: int = -1  # -1 = let backend decide
    timeout: float = 60.0
    vram_unload_below_mb: int = 0  # >0: unload polish model when free VRAM dips


@dataclass
class HotkeyConfig:
    keys: list[str] = field(default_factory=_default_hotkeys)
    mode: str = "hold"  # "hold" | "toggle"
    backend: str = "auto"  # "auto" | "evdev" | "pynput"


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    input_device: str = ""  # "" = system default; name or index
    # Short enough that quick commands ("yes period") survive; the RMS gate
    # already filters accidental taps.
    min_record_ms: int = 300
    max_record_ms: int = 120000
    muted_rms: float = 0.00001
    rms_gate: float = 0.005
    auto_stop_silence_s: float = 0.0  # >0: stop after this much trailing silence
    normalize: bool = True  # boost quiet/whispered speech before transcribing


@dataclass
class OutputConfig:
    mode: str = "paste"  # "paste" | "type"
    stream: bool = False  # live-type partial transcript while speaking
    stream_tick_ms: int = 600
    key_delay_ms: int = 0
    key_hold_ms: int = 0
    paste_chord: str = "auto"  # "auto" (Ctrl+V / Cmd+V) | "ctrl-v" | "ctrl-shift-v" | "cmd-v"
    newline_mode: str = "shift-enter"  # "shift-enter" | "enter"
    terminal_classes: list[str] = field(
        default_factory=lambda: [c.strip() for c in _DEFAULT_TERMINALS.split(",")]
    )


@dataclass
class DictionaryConfig:
    path: str = ""  # "" = <data_dir>/dictionary.txt
    words: list[str] = field(default_factory=list)
    use_selection: bool = True  # Linux: highlighted text becomes spelling context

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser() if self.path else data_dir() / "dictionary.txt"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""  # require Authorization: Bearer <token> when set


@dataclass
class UIConfig:
    beep: bool = True
    notify: bool = True
    tray: bool = False
    web_port: int = 8766  # the local web dashboard/settings UI
    open_browser: bool = True  # auto-open the UI when it starts


@dataclass
class HistoryConfig:
    enabled: bool = False
    path: str = ""  # "" = <data_dir>/history.jsonl

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser() if self.path else data_dir() / "history.jsonl"


@dataclass
class Config:
    engine: EngineConfig = field(default_factory=EngineConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    polish: PolishConfig = field(default_factory=PolishConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    dictionary: DictionaryConfig = field(default_factory=DictionaryConfig)
    replacements: dict[str, str] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


# Legacy env vars from the original prototype, kept working.
_LEGACY_ENV = {
    "WHISPER_MODEL": "whisper.model",
    "WHISPER_BACKEND": None,  # obsolete: faster-whisper only
    "WHISPER_DEVICE": "whisper.device",
    "WHISPER_COMPUTE": "whisper.compute",
    "OLLAMA_MODEL": "polish.model",
    "OLLAMA_URL": "polish.url",
    "POLISH_ENABLED": "polish.enabled",
    "POLISH_KEEP_ALIVE": "polish.keep_alive",
    "POLISH_NUM_CTX": "polish.num_ctx",
    "POLISH_NUM_PREDICT": "polish.num_predict",
    "POLISH_THINK": "polish.think",
    "POLISH_THINK_MIN_CHARS": "polish.think_min_chars",
    "POLISH_NUM_GPU": "polish.num_gpu",
    "POLISH_TIMEOUT": "polish.timeout",
    "VRAM_UNLOAD_BELOW_MB": "polish.vram_unload_below_mb",
    "HOTKEY_NAMES": "hotkey.keys",
    "MIN_RECORD_MS": "audio.min_record_ms",
    "MAX_RECORD_MS": "audio.max_record_ms",
    "MUTED_RMS": "audio.muted_rms",
    "RMS_GATE": "audio.rms_gate",
    "SAMPLE_RATE": "audio.sample_rate",
    "OUTPUT_MODE": "output.mode",
    "STREAM": "output.stream",
    "STREAM_TICK_MS": "output.stream_tick_ms",
    "KEY_DELAY_MS": "output.key_delay_ms",
    "KEY_HOLD_MS": "output.key_hold_ms",
    "NEWLINE_MODE": "output.newline_mode",
    "TERMINAL_CLASSES": "output.terminal_classes",
    "BEEP": "ui.beep",
}

_SECTIONS: dict[str, type] = {
    "engine": EngineConfig,
    "whisper": WhisperConfig,
    "polish": PolishConfig,
    "hotkey": HotkeyConfig,
    "audio": AudioConfig,
    "output": OutputConfig,
    "dictionary": DictionaryConfig,
    "server": ServerConfig,
    "ui": UIConfig,
    "history": HistoryConfig,
}


def _coerce(value: object, target_type: type) -> object:
    """Coerce a TOML/env/override value to the dataclass field type."""
    if target_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    if target_type is int:
        return int(str(value).strip())
    if target_type is float:
        return float(str(value).strip())
    if target_type is str:
        return str(value)
    if target_type is list or str(target_type).startswith("list"):
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [s.strip() for s in str(value).split(",") if s.strip()]
    return value


def _field_types(section_cls: type) -> dict[str, type]:
    out: dict[str, type] = {}
    for f in dataclasses.fields(section_cls):
        t = f.type if isinstance(f.type, type) else None
        if t is None:
            name = str(f.type)
            t = {"str": str, "int": int, "float": float, "bool": bool}.get(name, list)
        out[f.name] = t
    return out


def _suggest(name: str, candidates: list[str]) -> str:
    import difflib

    close = difflib.get_close_matches(name, candidates, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _set_dotted(cfg: Config, dotted: str, value: object) -> None:
    """Set e.g. 'whisper.model' on cfg, coercing the value."""
    section_name, _, key = dotted.partition(".")
    if section_name == "replacements":
        if isinstance(value, Mapping):
            cfg.replacements.update({str(k): str(v) for k, v in value.items()})
        return
    section_cls = _SECTIONS.get(section_name)
    if section_cls is None or not key:
        hint = _suggest(section_name, [*_SECTIONS, "replacements"])
        print(f"voicisst config: unknown section {section_name!r}{hint}", file=sys.stderr)
        return
    section = getattr(cfg, section_name)
    types = _field_types(section_cls)
    if key not in types:
        hint = _suggest(key, list(types))
        print(f"voicisst config: unknown key {dotted!r}{hint}", file=sys.stderr)
        return
    try:
        setattr(section, key, _coerce(value, types[key]))
    except (ValueError, TypeError) as e:  # bad value: keep default, warn
        print(f"voicisst config: ignoring {dotted}={value!r} ({e})", file=sys.stderr)


def _load_toml(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Config:
    """Build a Config. `overrides` keys are dotted ('whisper.model')."""
    env = os.environ if env is None else env
    cfg = Config()

    toml_path = Path(path) if path else config_path()
    if toml_path.is_file():
        try:
            data = _load_toml(toml_path)
        except Exception as e:
            print(f"voicisst config: failed to parse {toml_path}: {e}", file=sys.stderr)
            data = {}
        for section_name, section_val in data.items():
            if section_name == "replacements" and isinstance(section_val, Mapping):
                cfg.replacements.update({str(k): str(v) for k, v in section_val.items()})
            elif isinstance(section_val, Mapping):
                for key, value in section_val.items():
                    _set_dotted(cfg, f"{section_name}.{key}", value)

    for env_name, dotted in _LEGACY_ENV.items():
        if dotted and env_name in env:
            _set_dotted(cfg, dotted, env[env_name])

    for env_name, value in env.items():
        if not env_name.startswith("VOICISST_"):
            continue
        rest = env_name[len("VOICISST_") :].lower()
        section, _, key = rest.partition("_")
        if section in _SECTIONS and key:
            _set_dotted(cfg, f"{section}.{key}", value)

    for dotted, value in (overrides or {}).items():
        _set_dotted(cfg, dotted, value)

    return cfg


def default_config_toml() -> str:
    """A fully documented config template for `voicisst config init`."""
    hotkeys = ", ".join(f'"{k}"' for k in _default_hotkeys())
    return f'''# Voicisst configuration — https://github.com/lucyfromnaarm/voicisst
# Every setting is optional; these are the defaults. Environment variables
# (VOICISST_SECTION_KEY, e.g. VOICISST_WHISPER_MODEL) override this file.

[engine]
mode = "local"            # "local" = all-in-one | "remote" = use a voicisst server
server_url = ""           # e.g. "http://big-box:8765" (remote mode)
token = ""                # must match the server's token

[whisper]
model = "auto"            # "auto" -> large-v3-turbo (GPU) / small (CPU)
device = "auto"           # "auto" | "cuda" | "cpu"
language = "auto"         # "auto" detects any of 100+ languages; or "en", "es", ...

[polish]
enabled = true            # LLM cleanup: fillers, corrections, lists, punctuation
backend = "ollama"        # "ollama" | "openai" (any OpenAI-compatible server) | "none"
model = "qwen3.5:4b"
url = "http://localhost:11434"
api_key = ""              # for OpenAI-compatible backends that need one

[hotkey]
keys = [{hotkeys}]
mode = "hold"             # "hold" = push-to-talk | "toggle" = tap to start/stop

[audio]
min_record_ms = 300
max_record_ms = 120000
auto_stop_silence_s = 0.0 # >0: auto-stop after N seconds of silence (hands-free)
normalize = true          # boost quiet/whispered speech

[output]
mode = "paste"            # "paste" (fast, reliable) | "type" (per-keystroke)
stream = false            # live-type while you speak, then replace with polished text
paste_chord = "auto"      # "auto" | "ctrl-v" | "ctrl-shift-v" | "cmd-v"
newline_mode = "shift-enter"  # chat apps treat plain Enter as "send"

[dictionary]
# Names and jargon Voicisst should spell correctly. Also reads one-per-line
# words from dictionary.txt in the Voicisst data directory.
words = []
use_selection = true      # Linux: highlighted text guides spelling too

[replacements]
# Applied after polish, case-insensitive whole words. Example:
# "vs code" = "VS Code"

[server]
host = "127.0.0.1"        # set 0.0.0.0 to serve your LAN (set a token!)
port = 8765
token = ""

[ui]
beep = true
notify = true
web_port = 8766           # voicisst ui / voicisst run --ui (localhost only)
open_browser = true

[history]
enabled = false           # keep a local log of everything you dictate
'''
