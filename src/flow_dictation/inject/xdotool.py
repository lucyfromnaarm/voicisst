"""xdotool injector: X11 fallback when ydotool/ydotoold is unavailable.

xdotool talks to the X server directly, so it only works in real X11
sessions (not Wayland, not even XWayland for the focused-window case).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import Injector


class XdotoolInjector(Injector):
    """Inject keystrokes via xdotool (X11 only)."""

    name = "xdotool"

    @classmethod
    def available(cls) -> bool:
        if not shutil.which("xdotool"):
            return False
        if not os.environ.get("DISPLAY"):
            return False
        # xdotool cannot drive Wayland compositors; XWayland's DISPLAY is a
        # trap (events go to the wrong place or nowhere).
        if os.environ.get("WAYLAND_DISPLAY"):
            return False
        if os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland":
            return False
        return True

    # -- helpers ----------------------------------------------------------

    def _run(self, args: list[str], timeout: float) -> bool:
        try:
            subprocess.run(args, check=True, timeout=timeout)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    def _type_raw(self, text: str) -> bool:
        if not text:
            return True
        return self._run(
            ["xdotool", "type", "--delay", str(self.cfg.key_delay_ms), "--", text],
            timeout=30,
        )

    # -- Injector API -----------------------------------------------------

    def type_text(self, text: str) -> bool:
        """Type text; in shift-enter mode each '\\n' becomes Shift+Return."""
        if not text:
            return True
        if self.cfg.newline_mode != "shift-enter" or "\n" not in text:
            # xdotool types '\n' as Return itself in plain "enter" mode.
            return self._type_raw(text)
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if part and not self._type_raw(part):
                return False
            if i < len(parts) - 1 and not self._run(["xdotool", "key", "shift+Return"], timeout=5):
                return False
        return True

    def backspace(self, n: int) -> bool:
        """Send exactly n backspaces via `key --repeat n` (exact count)."""
        if n <= 0:
            return False
        return self._run(["xdotool", "key", "--repeat", str(n), "BackSpace"], timeout=30)

    def paste_chord(self) -> bool:
        if self.cfg.paste_chord == "ctrl-shift-v":
            chord = "ctrl+shift+v"
        else:  # "auto" / "ctrl-v" / anything else: plain Ctrl+V on X11
            chord = "ctrl+v"
        return self._run(["xdotool", "key", chord], timeout=5)

    def tap_escape(self) -> bool:
        return self._run(["xdotool", "key", "Escape"], timeout=2)
