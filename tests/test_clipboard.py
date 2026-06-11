"""Tests for flow_dictation.clipboard — all subprocess/which mocked."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from flow_dictation import clipboard


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
    fake_ctypes = MagicMock()  # every win32 call returns a truthy MagicMock
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    assert rec.calls == []  # ctypes succeeded; no PowerShell needed
    fake_ctypes.windll.user32.OpenClipboard.assert_called_once()
    fake_ctypes.windll.user32.SetClipboardData.assert_called_once()
    fake_ctypes.windll.user32.CloseClipboard.assert_called_once()


def test_copy_windows_powershell_fallback(monkeypatch):
    # Real (POSIX) ctypes has no .windll -> the ctypes path fails and the
    # PowerShell fallback runs.
    monkeypatch.setattr(sys, "platform", "win32")
    rec = RunRecorder()
    monkeypatch.setattr(clipboard.shutil, "which", which_factory("powershell"))
    monkeypatch.setattr(clipboard.subprocess, "run", rec)
    assert clipboard.copy("hi") is True
    args, kwargs = rec.calls[0]
    assert args[0] == "/usr/bin/powershell"
    assert "Set-Clipboard" in args[-1]
    assert kwargs["input"] == b"hi"


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
