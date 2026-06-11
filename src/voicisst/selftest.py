"""Environment diagnostics: `voicisst selftest` checks every layer with PASS/FAIL/SKIP.

Each step degrades gracefully headless — failures print the likely fix,
never a traceback. Exit code is 0 when nothing FAILed (SKIPs are fine).
A pre-built engine can be injected for tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import audio as audio_mod
from . import clipboard, inject
from .engine import get_engine

if TYPE_CHECKING:
    from .config import Config
    from .engine.base import Engine

_PROBE = "voicisst selftest clipboard probe"


class _Skip(Exception):
    """Raised by a step to mark itself SKIPped (reason in the message)."""


class _Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def step(self, name: str, fn: Callable[[], str | None]) -> None:
        sys.stderr.write(f"  {name}… ")
        sys.stderr.flush()
        try:
            detail = fn()
        except _Skip as s:
            self.skipped += 1
            print(f"SKIP ({s})", file=sys.stderr)
        except Exception as e:
            self.failed += 1
            message = f"FAIL: {e}"
            hint = getattr(e, "hint", "")
            if hint:
                message += f"\n      hint: {hint}"
            print(message, file=sys.stderr)
        else:
            self.passed += 1
            print("PASS" + (f" — {detail}" if detail else ""), file=sys.stderr)


def run_selftest(
    cfg: Config, *, engine: Engine | None = None, audio_seconds: float = 1.0
) -> int:
    """Run all diagnostic steps; return 0 (all good) or 1 (something failed)."""
    runner = _Runner()
    print("== voicisst selftest ==", file=sys.stderr)
    _print_config_summary(cfg)

    holder: dict[str, Any] = {}
    if engine is not None:
        holder["engine"] = engine

    runner.step("hotkeys: backend", lambda: _check_hotkeys(cfg))
    runner.step(f"audio: {audio_seconds:g}s capture", lambda: _check_audio(cfg, audio_seconds))
    runner.step("engine: health", lambda: _check_engine(cfg, holder))
    runner.step("injector: availability", lambda: _check_injector(cfg))
    runner.step("clipboard: copy round-trip", _check_clipboard)
    runner.step("polish: round-trip", lambda: _check_polish(cfg, holder))

    verdict = "OK" if runner.failed == 0 else "FAIL"
    print(
        f"== {verdict} ({runner.passed} passed, {runner.failed} failed, "
        f"{runner.skipped} skipped) ==",
        file=sys.stderr,
    )
    return 0 if runner.failed == 0 else 1


def _print_config_summary(cfg: Config) -> None:
    polish = f"{cfg.polish.backend}/{cfg.polish.model}" if cfg.polish.enabled else "disabled"
    target = f" ({cfg.engine.server_url})" if cfg.engine.mode == "remote" else ""
    stream = " +stream" if cfg.output.stream else ""
    print(
        f"  config: engine={cfg.engine.mode}{target} whisper={cfg.whisper.model} "
        f"polish={polish} hotkeys=[{', '.join(cfg.hotkey.keys)}] mode={cfg.hotkey.mode} "
        f"output={cfg.output.mode}{stream}",
        file=sys.stderr,
    )


# -- steps ------------------------------------------------------------------


def _check_hotkeys(cfg: Config) -> str:
    """Which backend get_listener would pick — without starting anything.

    On the evdev path this also verifies that some readable keyboard actually
    exposes one of the configured keycodes; a readable /dev/input alone says
    nothing about whether the hotkey can ever fire.
    """
    from .hotkeys.evdev_listener import INPUT_GROUP_HINT, EvdevListener
    from .hotkeys.pynput_listener import PynputListener

    backend = (cfg.hotkey.backend or "auto").strip().lower()
    keys = ", ".join(cfg.hotkey.keys)
    if backend == "evdev":
        if not EvdevListener.available():
            raise RuntimeError(f"evdev backend unusable — {INPUT_GROUP_HINT}")
        return f"evdev: {_check_evdev_keys(cfg)}"
    if backend == "pynput":
        if not PynputListener.available():
            raise RuntimeError(
                "pynput backend unusable (needs a desktop session; on Linux an X11 DISPLAY)"
            )
        return f"pynput would listen for [{keys}]"
    if sys.platform == "linux" and EvdevListener.available():
        return f"auto -> evdev: {_check_evdev_keys(cfg)}"
    if PynputListener.available():
        return f"auto -> pynput for [{keys}]"
    if sys.platform == "linux":
        raise RuntimeError(f"no usable hotkey backend — {INPUT_GROUP_HINT}")
    raise RuntimeError(
        "no usable hotkey backend — install pynput and grant input-monitoring permission"
    )


def _check_evdev_keys(cfg: Config) -> str:
    """Verify at least one readable input device exposes a configured key."""
    from .hotkeys.evdev_listener import INPUT_GROUP_HINT, EvdevListener

    listener = EvdevListener(cfg.hotkey.keys, lambda: None, lambda: None)
    try:
        devices, _codes = listener._find_devices()
    except PermissionError as e:
        raise RuntimeError(f"cannot read /dev/input ({e}) — {INPUT_GROUP_HINT}") from e
    for dev in devices:
        EvdevListener._close_quietly(dev)
    keys = ", ".join(cfg.hotkey.keys)
    if not devices:
        raise RuntimeError(
            f"no keyboard exposes {keys} — set hotkey.keys to a key you have "
            '(e.g. "KEY_RIGHTALT", "KEY_F9"); find names with python -m evdev.evtest'
        )
    return f"{len(devices)} device(s) expose [{keys}]"


def _check_audio(cfg: Config, seconds: float) -> str:
    try:
        recorder = audio_mod.Recorder(cfg.audio.sample_rate, cfg.audio.input_device)
    except Exception as e:
        raise RuntimeError(
            f"could not init audio capture: {e} — is PortAudio installed? "
            "(Debian/Ubuntu: `sudo apt install libportaudio2`); list devices "
            "with `python -m sounddevice`"
        ) from e
    recorder.start()
    time.sleep(seconds)
    audio_arr, _dur_ms = recorder.stop()
    if audio_arr.size == 0:
        raise RuntimeError(
            "captured 0 samples — microphone muted or wrong device? list devices "
            "with `python -m sounddevice` and set [audio] input_device in config.toml"
        )
    return f"{int(audio_arr.size)} samples @ {cfg.audio.sample_rate} Hz"


def _check_engine(cfg: Config, holder: dict[str, Any]) -> str:
    eng = holder.get("engine")
    if eng is None:
        eng = get_engine(cfg)
        holder["engine"] = eng
    if cfg.engine.mode != "remote":
        sys.stderr.write("(loading local models — the first run can take minutes) ")
        sys.stderr.flush()
        eng.warm()
    info = eng.health()
    if info.get("status") != "ok":
        raise RuntimeError(f"engine health not ok: {info}")
    return f"mode={info.get('mode')} whisper={info.get('whisper_model')}"


def _check_injector(cfg: Config) -> str:
    injector = inject.get_injector(cfg)
    return f"using {injector.name}"


def _read_clipboard() -> str | None:
    """Best-effort clipboard readback; None when no reader is available."""
    candidates: list[list[str]] = []
    if sys.platform.startswith("linux"):
        candidates = [["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"]]
    elif sys.platform == "darwin":
        candidates = [["pbpaste"]]
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=2, text=True)
            if r.returncode == 0:
                return r.stdout
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def _check_clipboard() -> str:
    if not clipboard.copy(_PROBE):
        raise RuntimeError(
            "clipboard copy failed — install wl-clipboard (Wayland) or xclip (X11); "
            "macOS uses pbcopy, Windows uses PowerShell"
        )
    readback = _read_clipboard()
    if readback is None:
        return "copied (no readback tool available)"
    if readback.strip() != _PROBE:
        raise RuntimeError(f"clipboard readback mismatch: {readback.strip()!r}")
    return "copy + readback OK"


def _check_polish(cfg: Config, holder: dict[str, Any]) -> str:
    if not cfg.polish.enabled or cfg.polish.backend.strip().lower() == "none":
        raise _Skip("polish disabled in config")
    eng = holder.get("engine")
    if eng is None:
        raise _Skip("engine unavailable (see the engine step)")
    if cfg.engine.mode != "remote":
        # Engines swallow polish failures at runtime (dictation must never
        # lose text), so the round-trip below can't fail on its own — probe
        # the backend directly first. In remote mode the polish backend lives
        # on the server, so the local polish.url says nothing about it.
        _probe_polish_backend(cfg)
    filler = "um hello world comma this is a test"
    sample = str(eng.polish(filler))
    if not sample.strip():
        raise RuntimeError("polish returned empty text")
    if sample.strip() == filler:
        raise RuntimeError(
            "polish returned its input unchanged — no LLM ran (runtime polish "
            "errors are swallowed; check the polish backend and model)"
        )
    return f"sample: {sample[:60]!r}"


def _probe_polish_backend(cfg: Config) -> None:
    """Talk to the polish backend directly; raise with a fix hint on failure."""
    import requests  # lazy: selftest must import headless

    backend = cfg.polish.backend.strip().lower()
    base = cfg.polish.url.rstrip("/")
    if backend == "ollama":
        try:
            r = requests.get(f"{base}/api/tags", timeout=3)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"ollama not running — systemctl status ollama (GET {base}/api/tags: {e})"
            ) from e
        tags = [m.get("name", "") for m in r.json().get("models", [])]
        if cfg.polish.model not in tags:
            raise RuntimeError(
                f"model not pulled — ollama pull {cfg.polish.model} (server has: {tags})"
            )
    elif backend == "openai":
        try:
            requests.get(base, timeout=3)
        except Exception as e:
            raise RuntimeError(
                f"openai-compatible server unreachable at {base} ({e}) — "
                "check polish.url in config.toml and that the server is running"
            ) from e
