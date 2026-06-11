"""Best-effort focused-window detection.

Used to spot terminals (where Voicisst copies instead of pasting, because the
paste chord and escape handling differ) and for the history log. Every
probe is wrapped and short-timeout'd: a missing tool or a hung compositor
must never stall dictation. Unknown is fine — return None.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence


def focused_window_class() -> str | None:
    """Class/app name of the focused window, or None if unknown."""
    if sys.platform == "darwin":
        return _frontmost_darwin()
    if sys.platform.startswith("win"):
        return _frontmost_windows()
    return _focused_linux()


def looks_like_terminal(cls: str | None, terminal_classes: Sequence[str]) -> bool:
    """True if `cls` matches any configured terminal class (substring,
    case-insensitive)."""
    if not cls:
        return False
    low = cls.lower()
    return any(t.lower() in low for t in terminal_classes if t.strip())


# -- Linux ----------------------------------------------------------------


def _focused_linux() -> str | None:
    # GNOME Wayland: no portable public API. This Shell DBus call works
    # only with the Window Calls (or similar) extension; harmless if absent.
    try:
        r = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell",
                "--object-path",
                "/org/gnome/Shell/Extensions/WindowsExt",
                "--method",
                "org.gnome.Shell.Extensions.WindowsExt.FocusClass",
            ],
            capture_output=True,
            timeout=0.5,
            text=True,
        )
        if r.returncode == 0:
            cls = r.stdout.strip().strip("(),'\"")
            if cls:
                return cls
    except (subprocess.SubprocessError, OSError):
        pass
    # Sway
    if shutil.which("swaymsg"):
        try:
            r = subprocess.run(
                ["swaymsg", "-t", "get_tree"], capture_output=True, timeout=0.5, text=True
            )
            if r.returncode == 0:
                hit = _sway_focused(json.loads(r.stdout))
                if hit:
                    return hit
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass
    # Hyprland
    if shutil.which("hyprctl"):
        try:
            r = subprocess.run(
                ["hyprctl", "-j", "activewindow"], capture_output=True, timeout=0.5, text=True
            )
            if r.returncode == 0:
                cls = (json.loads(r.stdout) or {}).get("class")
                if cls:
                    return cls
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass
    return None


def _sway_focused(node: dict) -> str | None:
    if node.get("focused"):
        return node.get("app_id") or (node.get("window_properties") or {}).get("class")
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        hit = _sway_focused(child)
        if hit:
            return hit
    return None


# -- macOS ----------------------------------------------------------------


def _frontmost_darwin() -> str | None:
    script = (
        'tell application "System Events" to get name of first '
        "application process whose frontmost is true"
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=1, text=True
        )
        if r.returncode == 0:
            name = r.stdout.strip()
            if name:
                return name
    except (subprocess.SubprocessError, OSError):
        pass
    return None


# -- Windows --------------------------------------------------------------


def _frontmost_windows() -> str | None:
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        # Process exe basename matches terminal_classes entries like
        # "cmd.exe" / "powershell" better than window titles do.
        exe = ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if handle:
                try:
                    size = ctypes.c_ulong(1024)
                    path_buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, path_buf, ctypes.byref(size)
                    ):
                        exe = (path_buf.value or "").rsplit("\\", 1)[-1]
                finally:
                    kernel32.CloseHandle(handle)
        return exe or title or None
    except Exception:
        return None
