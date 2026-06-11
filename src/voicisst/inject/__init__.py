"""Injector selection: pick the best way to type into the focused window.

Linux: ydotool (Wayland-safe, needs ydotoold) -> xdotool (X11) -> pynput.
macOS / Windows: pynput.
"""

from __future__ import annotations

import sys

from ..config import Config
from .base import Injector

__all__ = ["Injector", "get_injector"]


def get_injector(cfg: Config) -> Injector:
    """Return the first available injector for this platform.

    Raises RuntimeError with a platform-specific fix hint when none work.
    """
    from .pynput_injector import PynputInjector
    from .xdotool import XdotoolInjector
    from .ydotool import YdotoolInjector

    if sys.platform.startswith("linux"):
        candidates: list[type[Injector]] = [YdotoolInjector, XdotoolInjector, PynputInjector]
    else:  # darwin / win32
        candidates = [PynputInjector]

    for klass in candidates:
        try:
            if klass.available():
                return klass(cfg.output)
        except Exception as e:  # an availability probe must never kill selection
            print(f"injector {klass.name}: availability check failed: {e}", file=sys.stderr)

    raise RuntimeError(_no_injector_message())


def _no_injector_message() -> str:
    if sys.platform.startswith("linux"):
        return (
            "No text-injection backend available (tried ydotool, xdotool, pynput).\n"
            "Fix: install ydotool and start its daemon:\n"
            "    systemctl --user enable --now ydotoold\n"
            "You may also need the uinput udev rule and membership in the 'input'\n"
            "group (`sudo usermod -aG input $USER`, then log out and back in).\n"
            "scripts/setup-linux.sh sets all of this up for you.\n"
            "On X11 sessions, installing `xdotool` also works."
        )
    if sys.platform == "darwin":
        return (
            "No text-injection backend available (pynput could not be used).\n"
            "Fix: `pip install pynput`, then grant Accessibility permission to your\n"
            "terminal (or the Voicisst app) in System Settings -> Privacy & Security ->\n"
            "Accessibility. Without it macOS silently drops synthetic keystrokes."
        )
    return (
        "No text-injection backend available (pynput could not be used).\n"
        "Fix: `pip install pynput` and run Voicisst inside a regular desktop session."
    )
