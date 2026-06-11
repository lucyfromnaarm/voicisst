"""Tests for voicisst.clipboard — all subprocess/which mocked."""

from __future__ import annotations

import base64
import subprocess
import sys
from types import SimpleNamespace

import pytest

from voicisst import clipboard


class RunRecorder:
    """Replaces subprocess.run; records calls, scripted results."""

    def __init__(self, returncode: int = 0, stdout: str = "", fail_cmds: tuple = ()):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.fail_cmds = fail_cmds

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[0] in self.fail_cmds:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def which_factory(*names: str):
    return lambda name: f"/usr/bin/{name}" if name in names else None


class WinFunc:
    """Fake Win32 function: records calls and whether its ctypes signature
    (argtypes + restype) had been declared by the time it was invoked."""

    def __init__(self, result: object = 1):
        self.argtypes: tuple | None = None
        self.restype: object | None = None
        self.calls: list[tuple] = []
        self.typed_at_call: list[bool] = []
        self.result = result

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        self.typed_at_call.append(self.argtypes is not None and self.restype is not None)
        return self.result


# A handle value that does not fit in 32 bits: the bug this guards against is
# ctypes defaulting results to c_int and truncating 64-bit HGLOBAL/LPVOID.
HANDLE_64 = 0x7FFF_DEAD_BEEF_1234
LOCK_64 = 0x7FFF_DEAD_BEEF_2000


def make_fake_ctypes(
    *, alloc_result: int = HANDLE_64, lock_result: int = LOCK_64, set_result: int = 1
) -> SimpleNamespace:
    """A ctypes stand-in exposing WinDLL/wintypes/c_size_t/memmove like win32."""
    user32 = SimpleNamespace(
        OpenClipboard=WinFunc(1),
        EmptyClipboard=WinFunc(1),
        SetClipboardData=WinFunc(set_result),
        CloseClipboard=WinFunc(1),
    )
    kernel32 = SimpleNamespace(
        GlobalAlloc=WinFunc(alloc_result),
        GlobalLock=WinFunc(lock_result),
        GlobalUnlock=WinFunc(1),
        GlobalFree=WinFunc(1),
    )
    dlls = {"user32": user32, "kernel32": kernel32}
    memmoves: list[tuple] = []
    return SimpleNamespace(
        WinDLL=lambda name, **_kw: dlls[name],
        wintypes=SimpleNamespace(
            HWND="HWND",
            UINT="UINT",
            HANDLE="HANDLE",
            BOOL="BOOL",
            HGLOBAL="HGLOBAL",
            LPVOID="LPVOID",
        ),
        c_size_t="c_size_t",
        memmove=lambda ptr, data, n: memmoves.append((ptr, data, n)),
        # Test-side handles (the code under test never touches these):
        user32=user32,
        kernel32=kernel32,
        memmoves=memmoves,
    )


# -- copy -------------------------------------------------------------------


def test_copy_linux_prefers_wl_copy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("wl-copy", "xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("héllo") is True
    assert len(rec.calls) == 1
    args, kwargs = rec.calls[0]
    assert args == ["wl-copy"]
    assert kwargs["input"] == "héllo".encode()


def test_copy_linux_falls_back_to_xclip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    args, kwargs = rec.calls[0]
    assert args == ["xclip", "-selection", "clipboard"]
    assert kwargs["input"] == b"hi"


def test_copy_linux_xclip_after_wl_copy_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rec = RunRecorder(fail_cmds=("wl-copy",))
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("wl-copy", "xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    assert [c[0][0] for c in rec.calls] == ["wl-copy", "xclip"]


def test_copy_darwin_uses_pbcopy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("pbcopy"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    assert rec.calls[0][0] == ["pbcopy"]


def test_copy_no_tool_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", which_factory())
    monkeypatch.setattr(clipboard.subprocess, "run", RunRecorder())
    assert clipboard.copy("hi") is False
    assert "wl-clipboard" in capsys.readouterr().err


def test_copy_windows_ctypes_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = make_fake_ctypes()
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    assert rec.calls == []  # ctypes succeeded; no PowerShell needed
    assert fake.user32.OpenClipboard.calls == [(None,)]
    assert len(fake.user32.SetClipboardData.calls) == 1
    assert len(fake.user32.CloseClipboard.calls) == 1


def test_copy_windows_ctypes_declares_signatures_before_calling(monkeypatch):
    """64-bit safety: argtypes/restype must be set on every Win32 function
    BEFORE it is called, else HGLOBAL/LPVOID results truncate to c_int."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = make_fake_ctypes()
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(clipboard.subprocess, "run", RunRecorder())
    assert clipboard.copy("héllo ✓") is True
    called = set()
    for dll in (fake.user32, fake.kernel32):
        for name, fn in vars(dll).items():
            assert all(fn.typed_at_call), (
                f"{name} called before argtypes/restype were declared"
            )
            if fn.calls:
                called.add(name)
    assert {"OpenClipboard", "GlobalAlloc", "GlobalLock", "SetClipboardData"} <= called
    # Pointer-bearing signatures use the correct Win32 types.
    assert fake.kernel32.GlobalAlloc.argtypes == ("UINT", "c_size_t")
    assert fake.kernel32.GlobalAlloc.restype == "HGLOBAL"
    assert fake.kernel32.GlobalLock.argtypes == ("HGLOBAL",)
    assert fake.kernel32.GlobalLock.restype == "LPVOID"
    assert fake.kernel32.GlobalUnlock.argtypes == ("HGLOBAL",)
    assert fake.kernel32.GlobalFree.argtypes == ("HGLOBAL",)
    assert fake.user32.OpenClipboard.argtypes == ("HWND",)
    assert fake.user32.SetClipboardData.argtypes == ("UINT", "HANDLE")
    assert fake.user32.SetClipboardData.restype == "HANDLE"


def test_copy_windows_ctypes_64bit_handle_round_trip(monkeypatch):
    """Full-width handles voicisst GlobalAlloc -> GlobalLock/memmove ->
    SetClipboardData untruncated, and the payload is UTF-16LE + NUL."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = make_fake_ctypes()
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(clipboard.subprocess, "run", RunRecorder())
    text = "héllo — Grüße ✓"
    assert clipboard.copy(text) is True
    payload = text.encode("utf-16-le") + b"\x00\x00"
    assert fake.kernel32.GlobalAlloc.calls == [(0x0002, len(payload))]  # GMEM_MOVEABLE
    assert fake.kernel32.GlobalLock.calls == [(HANDLE_64,)]
    assert fake.memmoves == [(LOCK_64, payload, len(payload))]
    assert fake.user32.SetClipboardData.calls == [(13, HANDLE_64)]  # CF_UNICODETEXT
    assert fake.kernel32.GlobalFree.calls == []  # clipboard owns the handle now


def test_copy_windows_ctypes_alloc_failure_closes_and_falls_back(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = make_fake_ctypes(alloc_result=0)  # GlobalAlloc returns NULL
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("powershell"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True  # PowerShell fallback succeeded
    assert len(fake.user32.CloseClipboard.calls) == 1  # clipboard not leaked
    assert rec.calls[0][0][0] == "/usr/bin/powershell"


def test_copy_windows_ctypes_set_failure_frees_handle(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = make_fake_ctypes(set_result=0)  # SetClipboardData returns NULL
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(clipboard.shutil, "which", which_factory())
    monkeypatch.setattr(clipboard.subprocess, "run", RunRecorder())
    assert clipboard.copy("hi") is False
    assert fake.kernel32.GlobalFree.calls == [(HANDLE_64,)]  # not leaked
    assert len(fake.user32.CloseClipboard.calls) == 1


def test_copy_windows_powershell_fallback_uses_encoded_command_and_env(monkeypatch):
    # Real (POSIX) ctypes has no WinDLL -> the ctypes path fails and the
    # PowerShell fallback runs.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(clipboard.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("VOICISST_TEST_SENTINEL", "kept")
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("powershell"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    text = "héllo — Grüße ✓"  # would mojibake if piped through the OEM code page
    assert clipboard.copy(text) is True
    ((args, kwargs),) = rec.calls
    assert args[0] == "/usr/bin/powershell"
    assert "-EncodedCommand" in args
    assert "-Command" not in args  # no plain-text script, no quoting pitfalls
    encoded = args[args.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert script == "Set-Clipboard -Value $env:VOICISST_CLIP"
    # The text travels via the environment, never through console decoding.
    assert kwargs["env"]["VOICISST_CLIP"] == text
    assert kwargs["env"]["VOICISST_TEST_SENTINEL"] == "kept"  # parent env inherited
    assert "input" not in kwargs  # nothing piped to stdin at all
    assert kwargs["creationflags"] == 0x08000000  # CREATE_NO_WINDOW: no console flash


def test_copy_windows_powershell_creationflags_zero_without_attr(monkeypatch):
    # On platforms whose subprocess lacks CREATE_NO_WINDOW the flag must
    # degrade to 0 (so the code path stays testable/importable everywhere).
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(clipboard.subprocess, "CREATE_NO_WINDOW", raising=False)
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("pwsh"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    ((args, kwargs),) = rec.calls
    assert args[0] == "/usr/bin/pwsh"  # pwsh used when powershell is absent
    assert kwargs["creationflags"] == 0


def test_copy_windows_no_powershell(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(clipboard.shutil, "which", which_factory())
    monkeypatch.setattr(clipboard.subprocess, "run", RunRecorder())
    assert clipboard.copy("hi") is False
    assert "PowerShell" in capsys.readouterr().err


# -- read_primary_selection ---------------------------------------------------


def test_primary_selection_wl_paste(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rec = RunRecorder(stdout="picked text\n")
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("wl-paste", "xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.read_primary_selection() == "picked text"
    assert rec.calls[0][0] == ["wl-paste", "--primary", "--no-newline"]


def test_primary_selection_xclip_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rec = RunRecorder(stdout="sel")
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.read_primary_selection() == "sel"
    assert rec.calls[0][0] == ["xclip", "-o", "-selection", "primary"]


def test_primary_selection_wl_paste_empty_falls_to_xclip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def run(args, **kwargs):
        if args[0] == "wl-paste":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="from-x", stderr="")

    monkeypatch.setattr(clipboard.shutil, "which", which_factory("wl-paste", "xclip"))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert clipboard.read_primary_selection() == "from-x"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_primary_selection_non_linux_is_empty(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(
        clipboard.subprocess, "run", RunRecorder()
    )  # must not even be consulted
    assert clipboard.read_primary_selection() == ""


def test_primary_selection_failure_is_empty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("wl-paste"))

    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(clipboard.subprocess, "run", boom)
    assert clipboard.read_primary_selection() == ""
