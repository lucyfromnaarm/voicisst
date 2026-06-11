"""ydotool injector: types via the ydotoold uinput daemon (works on Wayland).

Ported from the original flow.py prototype, preserving its details:

- ydotool releases differ in which timing flags they accept; we probe
  `ydotool <subcmd> --help` once per (subcmd, flag) and cache the result.
- "shift-enter" newline mode sends Shift+Enter for each '\\n' so chat apps
  (Claude, Slack, Discord) don't treat Enter as submit.
- Backspace is emitted in batches of 128 press/release pairs — exactly n
  events total, never more.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

from .base import Injector

# evdev keycodes used below:
#   1 = KEY_ESC      14 = KEY_BACKSPACE   28 = KEY_ENTER
#  29 = KEY_LEFTCTRL 42 = KEY_LEFTSHIFT   47 = KEY_V

# Cached answers for "does `ydotool <subcmd>` support <flag>?".
_FLAG_CACHE: dict[tuple[str, str], bool] = {}

_FALLBACK_SOCKET = "/tmp/.ydotool_socket"


def _supports(subcmd: str, flag: str) -> bool:
    """Cached check: does `ydotool <subcmd> --help` mention `<flag>`?"""
    key = (subcmd, flag)
    if key in _FLAG_CACHE:
        return _FLAG_CACHE[key]
    supported = False
    if shutil.which("ydotool"):
        try:
            r = subprocess.run(
                ["ydotool", subcmd, "--help"],
                capture_output=True,
                timeout=3,
                text=True,
            )
            supported = flag in (r.stdout + r.stderr)
        except (subprocess.SubprocessError, OSError):
            supported = False
    _FLAG_CACHE[key] = supported
    return supported


def ydotoold_socket() -> str | None:
    """Path of a live ydotoold socket, or None.

    Candidates (in order): $YDOTOOL_SOCKET, $XDG_RUNTIME_DIR/.ydotool_socket,
    /tmp/.ydotool_socket — same probe the prototype selftest used.
    """
    candidates: list[str] = []
    sock = os.environ.get("YDOTOOL_SOCKET")
    if sock:
        candidates.append(sock)
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        candidates.append(os.path.join(rt, ".ydotool_socket"))
    candidates.append(_FALLBACK_SOCKET)
    for c in candidates:
        try:
            st = os.stat(c)
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode):
            return c
    return None


class YdotoolInjector(Injector):
    """Inject keystrokes through ydotool/ydotoold (Linux, Wayland-safe)."""

    name = "ydotool"

    @classmethod
    def available(cls) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        if not shutil.which("ydotool"):
            return False
        # The binary is useless without a running ydotoold daemon.
        return ydotoold_socket() is not None

    # -- helpers ----------------------------------------------------------

    def _timing_flags(self, subcmd: str) -> list[str]:
        """Build --key-delay / --key-hold args, omitting unsupported ones."""
        args: list[str] = []
        if _supports(subcmd, "--key-delay"):
            args += ["--key-delay", str(self.cfg.key_delay_ms)]
        if self.cfg.key_hold_ms > 0 and _supports(subcmd, "--key-hold"):
            args += ["--key-hold", str(self.cfg.key_hold_ms)]
        return args

    def _type_raw(self, text: str) -> bool:
        """Type a literal segment with no special newline handling."""
        if not shutil.which("ydotool") or not text:
            return not text
        try:
            subprocess.run(
                ["ydotool", "type", *self._timing_flags("type"), "--", text],
                check=True,
                timeout=30,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    def _shift_enter(self) -> bool:
        if not shutil.which("ydotool"):
            return False
        try:
            subprocess.run(
                ["ydotool", "key", *self._timing_flags("key"), "42:1", "28:1", "28:0", "42:0"],
                check=True,
                timeout=5,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    # -- Injector API -----------------------------------------------------

    def type_text(self, text: str) -> bool:
        """Type text, translating '\\n' to Shift+Enter in shift-enter mode."""
        if not text:
            return True
        if self.cfg.newline_mode != "shift-enter" or "\n" not in text:
            return self._type_raw(text)
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if part and not self._type_raw(part):
                return False
            if i < len(parts) - 1 and not self._shift_enter():
                return False
        return True

    def backspace(self, n: int) -> bool:
        """Send exactly n backspace key events, in batches of 128 pairs."""
        if n <= 0 or not shutil.which("ydotool"):
            return False
        sent = 0
        while sent < n:
            batch = min(n - sent, 128)
            args: list[str] = []
            for _ in range(batch):
                args.extend(["14:1", "14:0"])
            try:
                subprocess.run(
                    ["ydotool", "key", *self._timing_flags("key"), *args],
                    check=True,
                    timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                return False
            sent += batch
        return True

    def paste_chord(self) -> bool:
        """Press the configured paste chord (default Ctrl+V)."""
        if not shutil.which("ydotool"):
            return False
        if self.cfg.paste_chord == "ctrl-shift-v":
            keys = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
        else:  # "auto" / "ctrl-v" / anything else: plain Ctrl+V on Linux
            keys = ["29:1", "47:1", "47:0", "29:0"]
        try:
            subprocess.run(["ydotool", "key", *keys], check=True, timeout=5)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    def tap_escape(self) -> bool:
        """Best-effort Escape tap (clears a selection in most apps)."""
        if not shutil.which("ydotool"):
            return False
        try:
            subprocess.run(
                ["ydotool", "key", *self._timing_flags("key"), "1:1", "1:0"],
                check=True,
                timeout=2,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False
