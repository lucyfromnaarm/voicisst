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

_ACCESSIBILITY_HINT = (
    "macOS is silently dropping Flow's synthetic keystrokes.\n"
    "Grant Accessibility permission to your terminal (or the Flow app) in\n"
    "System Settings -> Privacy & Security -> Accessibility, then restart Flow."
)

# Process-wide: the Accessibility probe runs (and may hint) at most once,
# even across injector instances — a permanent condition, not worth spam.
_trust_probe_done = False


def _darwin_ax_trusted() -> bool | None:
    """Return AXIsProcessTrusted() on macOS, or None when unprobeable.

    Tries the pyobjc ``HIServices`` module first (it ships with pynput's
    macOS extras and is exactly what pynput's own listeners consult), then
    falls back to calling AXIsProcessTrusted via ctypes. Never raises; on
    Linux/Windows both probes simply fail and we report None.
    """
    try:
        import HIServices  # pyobjc-framework-ApplicationServices

        return bool(HIServices.AXIsProcessTrusted())
    except Exception:
        pass
    try:
        import ctypes
        import ctypes.util

        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        lib = ctypes.cdll.LoadLibrary(path)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def _warn_if_untrusted_darwin() -> None:
    """Surface the Accessibility hint once when macOS won't deliver events.

    CGEventPost without Accessibility permission is dropped *silently*:
    pynput raises nothing, every injection "succeeds", and the user sees no
    text. Probe AXIsProcessTrusted on the first injection and notify once.
    Non-darwin platforms never reach the probe.
    """
    global _trust_probe_done
    if sys.platform != "darwin" or _trust_probe_done:
        return
    _trust_probe_done = True  # set first: even a crashing probe never repeats
    try:
        untrusted = _darwin_ax_trusted() is False  # None = unprobeable, stay quiet
    except Exception:
        return  # the probe is best-effort diagnostics, never break injection
    if not untrusted:
        return
    try:
        from ..notify import notify

        notify("Accessibility permission missing", _ACCESSIBILITY_HINT, urgency="critical")
    except Exception:
        print(_ACCESSIBILITY_HINT, file=sys.stderr)


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
            _warn_if_untrusted_darwin()
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
                # pynput's win32 backend raises for astral (non-BMP) chars,
                # aborting Controller.type() mid-string — which would desync
                # StreamingTyper's last_typed mirror from the screen. We drop
                # those chars BEFORE typing: screen and mirror then agree on
                # everything we attempted, so returning True stays truthful.
                # (Completeness matters less than the mirror invariant.)
                # macOS's CGEventKeyboardSetUnicodeString handles non-BMP
                # text fine, so darwin/X11 keep the chars.
                if sys.platform.startswith("win"):
                    segment = "".join(c for c in segment if ord(c) <= 0xFFFF)
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
