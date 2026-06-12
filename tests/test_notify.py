"""Headless tests for voicisst.notify (subprocess mocked per platform)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from voicisst import notify as notify_mod


class PopenRecorder:
    """Stands in for subprocess.Popen; records calls, optionally fails some."""

    def __init__(self, fail_on: tuple[str, ...] = ()):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail_on = fail_on

    def __call__(self, cmd: list[str], **kwargs: object) -> object:
        if cmd[0] in self.fail_on:
            raise FileNotFoundError(cmd[0])
        self.calls.append((cmd, kwargs))
        return object()


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> PopenRecorder:
    rec = PopenRecorder()
    monkeypatch.setattr(notify_mod.subprocess, "Popen", rec)
    return rec


def test_always_logs_to_stderr_even_when_disabled(
    popen: PopenRecorder, capsys: pytest.CaptureFixture
) -> None:
    notify_mod.notify("hello", "world", enabled=False)
    assert "[hello] world" in capsys.readouterr().err
    assert popen.calls == []  # disabled: no desktop notification


def test_logs_to_stderr_when_enabled(
    popen: PopenRecorder, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    notify_mod.notify("voicisst ready", "hold the key")
    assert "[voicisst ready] hold the key" in capsys.readouterr().err


def test_linux_uses_notify_send_with_timeout(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    notify_mod.notify("Title", "Body", urgency="critical")
    ((cmd, kwargs),) = popen.calls
    assert cmd[0] == "notify-send"
    assert "Title" in cmd and "Body" in cmd
    i = cmd.index("-u")
    assert cmd[i + 1] == "critical"
    j = cmd.index("-t")
    assert int(cmd[j + 1]) > 0  # expire timeout so bubbles don't stack
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_linux_reuses_one_notification_slot(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every bubble carries the same replace-id, so a burst of events
    (rejected takes, repeated errors) updates ONE notification instead of
    stacking a new bubble per event."""
    monkeypatch.setattr(sys, "platform", "linux")
    notify_mod.notify("first", "a")
    notify_mod.notify("second", "b", urgency="critical")
    ids = []
    for cmd, _kwargs in popen.calls:
        i = cmd.index("-r")
        ids.append(cmd[i + 1])
    assert len(ids) == 2
    assert ids[0] == ids[1]
    assert int(ids[0]) > 0  # notify-send wants a positive uint32


def test_unknown_urgency_becomes_normal(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    notify_mod.notify("Title", "Body", urgency="shouty")
    ((cmd, _kwargs),) = popen.calls
    i = cmd.index("-u")
    assert cmd[i + 1] == "normal"


def test_darwin_osascript_escapes_double_quotes(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    notify_mod.notify('say "hi"', 'and "bye"')
    ((cmd, kwargs),) = popen.calls
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    script = cmd[2]
    assert "display notification" in script
    assert 'say \\"hi\\"' in script  # summary quotes escaped
    assert 'and \\"bye\\"' in script  # body quotes escaped
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_darwin_escapes_backslashes_before_quotes(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    notify_mod.notify("path C:\\tmp", "")
    script = popen.calls[0][0][2]
    assert "C:\\\\tmp" in script


# The AUMID Windows registers for Windows PowerShell; toasts from an
# unregistered AUMID (e.g. a bare 'Voicisst') are silently dropped.
_PS_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"


def test_windows_powershell_toast(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    notify_mod.notify("it's done", "body text")
    ((cmd, kwargs),) = popen.calls
    assert cmd[0] == "powershell"
    script = cmd[-1]
    assert "ToastNotification" in script
    assert "it''s done" in script  # single quotes doubled for PS strings
    assert "body text" in script
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_windows_toast_uses_registered_powershell_aumid(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    notify_mod.notify("Title", "Body")
    ((cmd, _kwargs),) = popen.calls
    script = cmd[-1]
    assert f"CreateToastNotifier('{_PS_AUMID}')" in script
    assert "CreateToastNotifier('Voicisst')" not in script  # unregistered: never displays


def test_windows_spawn_passes_create_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = PopenRecorder()
    monkeypatch.setattr(notify_mod.subprocess, "Popen", rec)
    monkeypatch.setattr(notify_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    notify_mod.notify("Title", "Body")
    ((_cmd, kwargs),) = rec.calls
    assert kwargs["creationflags"] == 0x08000000  # no console-window flash


def test_spawn_creationflags_degrade_to_zero_off_windows(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delattr(notify_mod.subprocess, "CREATE_NO_WINDOW", raising=False)
    notify_mod.notify("Title", "Body")
    ((_cmd, kwargs),) = popen.calls
    assert kwargs["creationflags"] == 0  # POSIX Popen only accepts 0


def test_spawn_works_with_real_popen_on_posix() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("POSIX-only sanity check")
    notify_mod._spawn(["true"])  # must not raise: creationflags=0 is legal on POSIX


def test_windows_falls_back_to_msg(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = PopenRecorder(fail_on=("powershell",))
    monkeypatch.setattr(notify_mod.subprocess, "Popen", rec)
    monkeypatch.setattr(sys, "platform", "win32")
    notify_mod.notify("Title", "Body")
    ((cmd, _kwargs),) = rec.calls
    assert cmd[0] == "msg"
    assert any("Title" in part and "Body" in part for part in cmd)


def test_unknown_platform_spawns_nothing(
    popen: PopenRecorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "platform", "sunos5")
    notify_mod.notify("a", "b")
    assert popen.calls == []
    assert "[a] b" in capsys.readouterr().err  # stderr log still happens


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_never_raises_when_popen_explodes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, platform: str
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("no such binary")

    monkeypatch.setattr(notify_mod.subprocess, "Popen", boom)
    monkeypatch.setattr(sys, "platform", platform)
    notify_mod.notify("summary", "body")  # must not raise
    assert "[summary] body" in capsys.readouterr().err
