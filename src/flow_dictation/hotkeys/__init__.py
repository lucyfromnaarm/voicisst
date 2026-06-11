"""Hotkey backend selection.

- evdev: Linux raw kernel input (works on Wayland *and* X11; needs read
  access to /dev/input — the 'input' group).
- pynput: macOS, Windows, and Linux/X11 sessions.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..engine.base import EngineError
from .base import HotkeyListener

if TYPE_CHECKING:
    from ..config import Config

__all__ = ["HotkeyListener", "get_listener"]

_MACWIN_HINT = (
    "install pynput (pip install pynput); on macOS also grant your terminal/app "
    "Input Monitoring permission in System Settings -> Privacy & Security"
)


def get_listener(
    cfg: Config,
    on_press: Callable[[], None],
    on_release: Callable[[], None],
    on_backspace: Callable[[], None] | None = None,
) -> HotkeyListener:
    """Build the hotkey listener for cfg.hotkey.backend ("auto" picks one).

    Linux "auto" prefers evdev (works on Wayland, sees all keyboards) and
    falls back to pynput (X11 only). macOS/Windows use pynput.
    """
    from .evdev_listener import INPUT_GROUP_HINT, EvdevListener
    from .pynput_listener import PynputListener

    backend = (cfg.hotkey.backend or "auto").strip().lower()
    args = (cfg.hotkey.keys, on_press, on_release, on_backspace)

    if backend == "evdev":
        if sys.platform != "linux":
            raise EngineError(
                f"hotkey backend 'evdev' only works on Linux (this is {sys.platform})",
                hint='set hotkey.backend = "pynput" or "auto"',
            )
        if not EvdevListener.available():
            raise EngineError(
                "hotkey backend 'evdev' cannot read any /dev/input device",
                hint=INPUT_GROUP_HINT,
            )
        return EvdevListener(*args)

    if backend == "pynput":
        if not PynputListener.available():
            if sys.platform == "linux":
                hint = (
                    "pynput needs an X11 session (DISPLAY is unset — pure Wayland?); "
                    'use hotkey.backend = "evdev" instead'
                )
            else:
                hint = _MACWIN_HINT
            raise EngineError("hotkey backend 'pynput' is not usable here", hint=hint)
        return PynputListener(*args)

    if backend != "auto":
        raise EngineError(
            f"unknown hotkey backend {backend!r}",
            hint='hotkey.backend must be "auto", "evdev", or "pynput"',
        )

    # auto
    if sys.platform == "linux":
        if EvdevListener.available():
            return EvdevListener(*args)
        if PynputListener.available():
            return PynputListener(*args)
        raise EngineError(
            "no usable hotkey backend: evdev cannot read /dev/input, and pynput "
            "has no X11 DISPLAY",
            hint=INPUT_GROUP_HINT,
        )
    if PynputListener.available():
        return PynputListener(*args)
    raise EngineError("no usable hotkey backend (pynput unavailable)", hint=_MACWIN_HINT)
