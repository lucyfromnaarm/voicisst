"""Cross-platform clipboard helpers.

`copy()` puts text on the system clipboard; `read_primary_selection()`
reads the Linux PRIMARY selection (whatever is currently highlighted),
which Voicisst feeds to Whisper/the polisher as spelling context.

Everything here is best-effort: failures return False/"" with a hint on
stderr, never an exception.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys


def copy(text: str) -> bool:
    """Put `text` on the system clipboard. Returns True on success.

    Tool order is platform-appropriate:
      Linux/BSD: wl-copy (Wayland) -> xclip (X11)
      macOS:     pbcopy
      Windows:   ctypes Win32 clipboard (robust Unicode), PowerShell fallback
    """
    if sys.platform.startswith("win"):
        return _copy_windows(text)
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    else:
        commands = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for cmd in commands:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
    if sys.platform == "darwin":
        print("clipboard copy failed — pbcopy did not work", file=sys.stderr)
    else:
        print(
            "clipboard copy failed — install wl-clipboard (Wayland) or xclip (X11)",
            file=sys.stderr,
        )
    return False


def _copy_windows(text: str) -> bool:
    """Windows copy: ctypes Win32 API first (no console flash, correct
    Unicode), then PowerShell `Set-Clipboard` as a fallback."""
    if _copy_windows_ctypes(text):
        return True
    return _copy_windows_powershell(text)


def _copy_windows_ctypes(text: str) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        # Local WinDLL instances with explicit signatures. The bare
        # ctypes.windll shortcut defaults every argument and result to a
        # 32-bit c_int, which truncates HGLOBAL/LPVOID pointers on 64-bit
        # Python (heap corruption / silent failure). Local instances also
        # avoid mutating the process-wide ctypes.windll function cache.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard.argtypes = (wintypes.HWND,)
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = ()
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = ()
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            # The clipboard owns the handle after SetClipboardData succeeds.
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


# PowerShell reads the text from this environment variable; passing it via
# the environment sidesteps stdin code-page decoding (mojibake) and any
# command-line quoting pitfalls.
_PS_CLIP_ENV = "VOICISST_CLIP"
_PS_CLIP_SCRIPT = f"Set-Clipboard -Value $env:{_PS_CLIP_ENV}"


def _copy_windows_powershell(text: str) -> bool:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        print("clipboard copy failed — PowerShell not found on PATH", file=sys.stderr)
        return False
    # -EncodedCommand takes base64(UTF-16LE), so the script itself is never
    # run through the console's OEM code page either.
    encoded = base64.b64encode(_PS_CLIP_SCRIPT.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ)
    env[_PS_CLIP_ENV] = text
    try:
        subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=env,
            check=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        print("clipboard copy failed — `Set-Clipboard` errored", file=sys.stderr)
        return False


def read_primary_selection() -> str:
    """Currently highlighted text (Linux PRIMARY selection), else ''.

    Tries wl-paste (Wayland) then xclip (X11). Non-Linux platforms have
    no primary selection: returns '' immediately.
    """
    if not sys.platform.startswith("linux"):
        return ""
    if shutil.which("wl-paste"):
        try:
            r = subprocess.run(
                ["wl-paste", "--primary", "--no-newline"],
                capture_output=True,
                timeout=1,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    if shutil.which("xclip"):
        try:
            r = subprocess.run(
                ["xclip", "-o", "-selection", "primary"],
                capture_output=True,
                timeout=1,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    return ""
