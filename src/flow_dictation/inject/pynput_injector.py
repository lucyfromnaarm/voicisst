"""pynput injector: macOS / Windows primary backend, X11 last resort.

pynput is imported lazily (inside methods) so this module is importable
headless. Every pynput call is wrapped: environmental failures (no
Accessibility permission on macOS, no X connection, ...) return False,
never raise.
"""

from __future__ import annotations

import os
import sys

from ..config import OutputConfig
from .base import Injector


class PynputInjector(Injector):
    """Inject keystrokes via pynput's keyboard Controller."""

    name = "pynput"

    def __init__(self, output_cfg: OutputConfig):
        super().__init__(output_cfg)
        self._kb = None  # lazily-built pynput Controller

    @classmethod
    def available(cls) -> bool:
        ok_platform = (
            sys.platform == "darwin"
            or sys.platform.startswith("win")
            or bool(os.environ.get("DISPLAY"))
        )
        if not ok_platform:
            return False
        try:
            import pynput  # noqa: F401
        except Exception:
            return False
        return True

    def _controller(self):
        if self._kb is None:
            from pynput.keyboard import Controller

            self._kb = Controller()
        return self._kb

    # -- Injector API -----------------------------------------------------

    def type_text(self, text: str) -> bool:
        """Type text; newlines honor cfg.newline_mode (shift-enter or enter)."""
        if not text:
            return True
        try:
            from pynput.keyboard import Key

            kb = self._controller()
            segments = text.split("\n")
            for i, segment in enumerate(segments):
                if segment:
                    kb.type(segment)
                if i < len(segments) - 1:
                    if self.cfg.newline_mode == "shift-enter":
                        kb.press(Key.shift)
                        kb.press(Key.enter)
                        kb.release(Key.enter)
                        kb.release(Key.shift)
                    else:
                        kb.press(Key.enter)
                        kb.release(Key.enter)
            return True
        except Exception as e:
            print(f"pynput type failed: {e}", file=sys.stderr)
            return False

    def backspace(self, n: int) -> bool:
        """Tap Backspace exactly n times."""
        if n <= 0:
            return False
        try:
            from pynput.keyboard import Key

            kb = self._controller()
            for _ in range(n):
                kb.press(Key.backspace)
                kb.release(Key.backspace)
            return True
        except Exception as e:
            print(f"pynput backspace failed: {e}", file=sys.stderr)
            return False

    def paste_chord(self) -> bool:
        """Press the paste shortcut: Cmd+V on macOS, Ctrl+V elsewhere,
        unless cfg.paste_chord overrides it."""
        try:
            from pynput.keyboard import Key

            kb = self._controller()
            chord = self.cfg.paste_chord
            if chord == "auto":
                chord = "cmd-v" if sys.platform == "darwin" else "ctrl-v"
            if chord == "cmd-v":
                mods = [Key.cmd]
            elif chord == "ctrl-shift-v":
                mods = [Key.ctrl, Key.shift]
            else:  # "ctrl-v" / anything else
                mods = [Key.ctrl]
            for mod in mods:
                kb.press(mod)
            kb.press("v")
            kb.release("v")
            for mod in reversed(mods):
                kb.release(mod)
            return True
        except Exception as e:
            print(f"pynput paste failed: {e}", file=sys.stderr)
            return False

    def tap_escape(self) -> bool:
        try:
            from pynput.keyboard import Key

            kb = self._controller()
            kb.press(Key.esc)
            kb.release(Key.esc)
            return True
        except Exception as e:
            print(f"pynput escape failed: {e}", file=sys.stderr)
            return False
