"""Tests for the injector backends — subprocess and pynput fully mocked."""

from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicisst import notify as notify_mod
from voicisst.config import OutputConfig, load_config
from voicisst.inject import get_injector
from voicisst.inject import pynput_injector as pyn_mod
from voicisst.inject import windowinfo as wi
from voicisst.inject import xdotool as xdo_mod
from voicisst.inject import ydotool as ydo_mod
from voicisst.inject.pynput_injector import PynputInjector
from voicisst.inject.xdotool import XdotoolInjector
from voicisst.inject.ydotool import YdotoolInjector


class RunRecorder:
    """subprocess.run replacement: records commands, answers --help probes."""

    def __init__(self, help_text: str = "--key-delay --key-hold"):
        self.calls: list[tuple[list[str], dict]] = []
        self.help_text = help_text

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs))
        if "--help" in args:
            return SimpleNamespace(returncode=0, stdout=self.help_text, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def cmds(self) -> list[list[str]]:
        return [c for c, _ in self.calls if "--help" not in c]

    @property
    def help_cmds(self) -> list[list[str]]:
        return [c for c, _ in self.calls if "--help" in c]


def which_factory(*names: str):
    return lambda name: f"/usr/bin/{name}" if name in names else None


# =========================================================================
# ydotool
# =========================================================================


@pytest.fixture
def ydo(monkeypatch):
    ydo_mod._FLAG_CACHE.clear()
    rec = RunRecorder()
    monkeypatch.setattr(ydo_mod.shutil, "which", which_factory("ydotool"))
    monkeypatch.setattr(ydo_mod.subprocess, "run", rec)
    yield rec
    ydo_mod._FLAG_CACHE.clear()


def test_ydotool_type_simple(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.type_text("hello world") is True
    assert ydo.cmds == [["ydotool", "type", "--key-delay", "0", "--", "hello world"]]


def test_ydotool_type_empty_is_noop(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.type_text("") is True
    assert ydo.calls == []


def test_ydotool_flag_probe_cached(ydo):
    inj = YdotoolInjector(OutputConfig())
    inj.type_text("a")
    inj.type_text("b")
    # exactly one --help probe for the "type" subcommand, then cached
    assert ydo.help_cmds == [["ydotool", "type", "--help"]]
    assert ydo_mod._FLAG_CACHE[("type", "--key-delay")] is True


def test_ydotool_no_timing_flag_support(monkeypatch):
    ydo_mod._FLAG_CACHE.clear()
    rec = RunRecorder(help_text="no flags here")
    monkeypatch.setattr(ydo_mod.shutil, "which", which_factory("ydotool"))
    monkeypatch.setattr(ydo_mod.subprocess, "run", rec)
    inj = YdotoolInjector(OutputConfig())
    assert inj.type_text("hi") is True
    assert rec.cmds == [["ydotool", "type", "--", "hi"]]
    ydo_mod._FLAG_CACHE.clear()


def test_ydotool_key_hold_flag(ydo):
    inj = YdotoolInjector(OutputConfig(key_delay_ms=2, key_hold_ms=12))
    inj.type_text("x")
    assert ydo.cmds == [
        ["ydotool", "type", "--key-delay", "2", "--key-hold", "12", "--", "x"]
    ]


def test_ydotool_newline_shift_enter(ydo):
    inj = YdotoolInjector(OutputConfig(newline_mode="shift-enter"))
    assert inj.type_text("a\nb\n") is True
    assert ydo.cmds == [
        ["ydotool", "type", "--key-delay", "0", "--", "a"],
        ["ydotool", "key", "--key-delay", "0", "42:1", "28:1", "28:0", "42:0"],
        ["ydotool", "type", "--key-delay", "0", "--", "b"],
        ["ydotool", "key", "--key-delay", "0", "42:1", "28:1", "28:0", "42:0"],
    ]


def test_ydotool_newline_enter_mode_types_raw(ydo):
    inj = YdotoolInjector(OutputConfig(newline_mode="enter"))
    assert inj.type_text("a\nb") is True
    assert ydo.cmds == [["ydotool", "type", "--key-delay", "0", "--", "a\nb"]]


def test_ydotool_backspace_exact_count(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.backspace(3) is True
    assert len(ydo.cmds) == 1
    cmd = ydo.cmds[0]
    assert cmd[:4] == ["ydotool", "key", "--key-delay", "0"]
    assert cmd[4:] == ["14:1", "14:0"] * 3


def test_ydotool_backspace_batches_of_128(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.backspace(300) is True
    batches = [cmd for cmd in ydo.cmds if cmd[1] == "key"]
    sizes = [cmd.count("14:1") for cmd in batches]
    assert sizes == [128, 128, 44]
    assert sum(sizes) == 300
    total_releases = sum(cmd.count("14:0") for cmd in batches)
    assert total_releases == 300


def test_ydotool_backspace_zero_or_negative(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.backspace(0) is False
    assert inj.backspace(-2) is False
    assert ydo.calls == []


@pytest.mark.parametrize(
    "chord,expected",
    [
        ("auto", ["29:1", "47:1", "47:0", "29:0"]),
        ("ctrl-v", ["29:1", "47:1", "47:0", "29:0"]),
        ("ctrl-shift-v", ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]),
    ],
)
def test_ydotool_paste_chords(ydo, chord, expected):
    inj = YdotoolInjector(OutputConfig(paste_chord=chord))
    assert inj.paste_chord() is True
    assert ydo.cmds == [["ydotool", "key", *expected]]


def test_ydotool_tap_escape(ydo):
    inj = YdotoolInjector(OutputConfig())
    assert inj.tap_escape() is True
    assert ydo.cmds == [["ydotool", "key", "--key-delay", "0", "1:1", "1:0"]]


def test_ydotool_failure_returns_false(monkeypatch):
    ydo_mod._FLAG_CACHE.clear()
    monkeypatch.setattr(ydo_mod.shutil, "which", which_factory("ydotool"))

    def boom(args, **kwargs):
        if "--help" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise OSError("ydotoold gone")

    monkeypatch.setattr(ydo_mod.subprocess, "run", boom)
    inj = YdotoolInjector(OutputConfig())
    assert inj.type_text("hi") is False
    assert inj.backspace(2) is False
    assert inj.paste_chord() is False
    assert inj.tap_escape() is False
    ydo_mod._FLAG_CACHE.clear()


def test_ydotool_available_needs_binary_and_socket(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ydo_mod.sys, "platform", "linux")
    monkeypatch.setattr(ydo_mod.shutil, "which", which_factory("ydotool"))
    sock_path = tmp_path / "ydo.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.close()  # the filesystem socket inode persists
    monkeypatch.setenv("YDOTOOL_SOCKET", str(sock_path))
    assert YdotoolInjector.available() is True

    # plain file is not a socket
    plain = tmp_path / "not-a-socket"
    plain.write_text("x")
    monkeypatch.setenv("YDOTOOL_SOCKET", str(plain))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ydo_mod, "_FALLBACK_SOCKET", str(tmp_path / "missing"))
    assert YdotoolInjector.available() is False

    # XDG_RUNTIME_DIR candidate works too
    monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
    rt_sock = tmp_path / ".ydotool_socket"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(rt_sock))
    s.close()
    assert YdotoolInjector.available() is True

    # no binary -> unavailable regardless of socket
    monkeypatch.setattr(ydo_mod.shutil, "which", which_factory())
    assert YdotoolInjector.available() is False


def test_ydotool_unavailable_off_linux(monkeypatch):
    monkeypatch.setattr(ydo_mod.sys, "platform", "darwin")
    assert YdotoolInjector.available() is False


# =========================================================================
# xdotool
# =========================================================================


@pytest.fixture
def xdo(monkeypatch):
    rec = RunRecorder()
    monkeypatch.setattr(xdo_mod.shutil, "which", which_factory("xdotool"))
    monkeypatch.setattr(xdo_mod.subprocess, "run", rec)
    return rec


def test_xdotool_available_x11_only(monkeypatch):
    monkeypatch.setattr(xdo_mod.shutil, "which", which_factory("xdotool"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert XdotoolInjector.available() is True

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert XdotoolInjector.available() is False

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert XdotoolInjector.available() is False

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert XdotoolInjector.available() is False

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(xdo_mod.shutil, "which", which_factory())
    assert XdotoolInjector.available() is False


def test_xdotool_type_with_delay(xdo):
    inj = XdotoolInjector(OutputConfig(key_delay_ms=5))
    assert inj.type_text("hello") is True
    assert xdo.cmds == [["xdotool", "type", "--delay", "5", "--", "hello"]]


def test_xdotool_newline_shift_enter(xdo):
    inj = XdotoolInjector(OutputConfig(newline_mode="shift-enter"))
    assert inj.type_text("a\nb") is True
    assert xdo.cmds == [
        ["xdotool", "type", "--delay", "0", "--", "a"],
        ["xdotool", "key", "shift+Return"],
        ["xdotool", "type", "--delay", "0", "--", "b"],
    ]


def test_xdotool_newline_enter_mode(xdo):
    inj = XdotoolInjector(OutputConfig(newline_mode="enter"))
    assert inj.type_text("a\nb") is True
    assert xdo.cmds == [["xdotool", "type", "--delay", "0", "--", "a\nb"]]


def test_xdotool_backspace_uses_repeat(xdo):
    inj = XdotoolInjector(OutputConfig())
    assert inj.backspace(7) is True
    assert xdo.cmds == [["xdotool", "key", "--repeat", "7", "BackSpace"]]
    assert inj.backspace(0) is False
    assert len(xdo.cmds) == 1  # no extra command for n=0


@pytest.mark.parametrize(
    "chord,key",
    [("auto", "ctrl+v"), ("ctrl-v", "ctrl+v"), ("ctrl-shift-v", "ctrl+shift+v")],
)
def test_xdotool_paste_chords(xdo, chord, key):
    inj = XdotoolInjector(OutputConfig(paste_chord=chord))
    assert inj.paste_chord() is True
    assert xdo.cmds == [["xdotool", "key", key]]


def test_xdotool_escape(xdo):
    inj = XdotoolInjector(OutputConfig())
    assert inj.tap_escape() is True
    assert xdo.cmds == [["xdotool", "key", "Escape"]]


# =========================================================================
# pynput
# =========================================================================


class FakeKey:
    shift = "<shift>"
    enter = "<enter>"
    backspace = "<backspace>"
    esc = "<esc>"
    ctrl = "<ctrl>"
    cmd = "<cmd>"


@pytest.fixture
def fake_pynput(monkeypatch):
    events: list[tuple[str, object]] = []

    class FakeController:
        def type(self, s):
            events.append(("type", s))

        def press(self, k):
            events.append(("press", k))

        def release(self, k):
            events.append(("release", k))

    kb_mod = types.ModuleType("pynput.keyboard")
    kb_mod.Controller = FakeController
    kb_mod.Key = FakeKey
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb_mod
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb_mod)
    return events


def test_pynput_available(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert PynputInjector.available() is False

    monkeypatch.setenv("DISPLAY", ":0")
    assert PynputInjector.available() is True

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert PynputInjector.available() is True
    monkeypatch.setattr(sys, "platform", "win32")
    assert PynputInjector.available() is True


def test_pynput_unavailable_when_import_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "pynput", None)  # forces ImportError
    assert PynputInjector.available() is False


def test_pynput_type_shift_enter(fake_pynput):
    inj = PynputInjector(OutputConfig(newline_mode="shift-enter"))
    assert inj.type_text("a\nb") is True
    assert fake_pynput == [
        ("type", "a"),
        ("press", FakeKey.shift),
        ("press", FakeKey.enter),
        ("release", FakeKey.enter),
        ("release", FakeKey.shift),
        ("type", "b"),
    ]


def test_pynput_type_enter_mode(fake_pynput):
    inj = PynputInjector(OutputConfig(newline_mode="enter"))
    assert inj.type_text("a\nb") is True
    assert fake_pynput == [
        ("type", "a"),
        ("press", FakeKey.enter),
        ("release", FakeKey.enter),
        ("type", "b"),
    ]


def test_pynput_backspace_taps(fake_pynput):
    inj = PynputInjector(OutputConfig())
    assert inj.backspace(3) is True
    assert fake_pynput == [("press", FakeKey.backspace), ("release", FakeKey.backspace)] * 3
    assert inj.backspace(0) is False


def test_pynput_paste_auto_linux(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "linux")
    inj = PynputInjector(OutputConfig(paste_chord="auto"))
    assert inj.paste_chord() is True
    assert fake_pynput == [
        ("press", FakeKey.ctrl),
        ("press", "v"),
        ("release", "v"),
        ("release", FakeKey.ctrl),
    ]


def test_pynput_paste_auto_darwin(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "darwin")
    inj = PynputInjector(OutputConfig(paste_chord="auto"))
    assert inj.paste_chord() is True
    assert fake_pynput == [
        ("press", FakeKey.cmd),
        ("press", "v"),
        ("release", "v"),
        ("release", FakeKey.cmd),
    ]


def test_pynput_paste_ctrl_shift_v_override(fake_pynput):
    inj = PynputInjector(OutputConfig(paste_chord="ctrl-shift-v"))
    assert inj.paste_chord() is True
    assert fake_pynput == [
        ("press", FakeKey.ctrl),
        ("press", FakeKey.shift),
        ("press", "v"),
        ("release", "v"),
        ("release", FakeKey.shift),
        ("release", FakeKey.ctrl),
    ]


def test_pynput_escape(fake_pynput):
    inj = PynputInjector(OutputConfig())
    assert inj.tap_escape() is True
    assert fake_pynput == [("press", FakeKey.esc), ("release", FakeKey.esc)]


# -- astral (non-BMP) characters ------------------------------------------
#
# pynput's win32 backend raises ValueError for ord(char) > 0xFFFF and
# Controller.type() aborts mid-string. type_text must pre-filter astral
# chars on win32 (screen == what we attempted, mirror stays honest) and
# keep them everywhere else.


def test_pynput_win32_drops_astral_chars(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "win32")
    inj = PynputInjector(OutputConfig(newline_mode="enter"))
    assert inj.type_text("a\U0001f600b\nc\U0001f4a9d") is True
    assert fake_pynput == [
        ("type", "ab"),
        ("press", FakeKey.enter),
        ("release", FakeKey.enter),
        ("type", "cd"),
    ]


def test_pynput_win32_astral_only_text_types_nothing_returns_true(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "win32")
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("\U0001f600\U0001f680") is True
    assert fake_pynput == []  # nothing attempted, truthfully "all attempted" typed


def test_pynput_win32_strict_controller_never_sees_astral(monkeypatch):
    """Simulate the real pynput-win32 Controller: raises on the first
    non-BMP char, having already typed the prefix (the desync this finding
    is about). With pre-filtering it must never get the chance."""
    typed: list[str] = []

    class Win32Controller:
        def type(self, s):
            for i, c in enumerate(s):
                if ord(c) > 0xFFFF:
                    typed.append(s[:i])  # prefix reached the screen anyway
                    raise ValueError(f"unsupported character: {c!r}")
            typed.append(s)

        def press(self, k):
            pass

        def release(self, k):
            pass

    kb_mod = types.ModuleType("pynput.keyboard")
    kb_mod.Controller = Win32Controller
    kb_mod.Key = FakeKey
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb_mod
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb_mod)
    monkeypatch.setattr(sys, "platform", "win32")

    inj = PynputInjector(OutputConfig())
    assert inj.type_text("héllo \U0001f600 world") is True
    assert typed == ["héllo  world"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_pynput_non_windows_keeps_astral_chars(monkeypatch, fake_pynput, platform):
    # macOS CGEventKeyboardSetUnicodeString (and X11) handle non-BMP text.
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", True)  # isolate from probe
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("a\U0001f600b") is True
    assert fake_pynput == [("type", "a\U0001f600b")]


# -- macOS Accessibility trust probe ---------------------------------------
#
# CGEventPost without Accessibility permission is dropped silently while
# type/paste return True. The first injection on darwin probes
# AXIsProcessTrusted and surfaces the hint exactly once.


@pytest.fixture
def notify_spy(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def fake_notify(summary, body="", urgency="low", *, enabled=True):
        calls.append((summary, body, urgency))

    monkeypatch.setattr(notify_mod, "notify", fake_notify)
    return calls


def test_pynput_darwin_untrusted_hints_once(monkeypatch, fake_pynput, notify_spy):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", False)
    probes: list[int] = []

    def fake_probe():
        probes.append(1)
        return False

    monkeypatch.setattr(pyn_mod, "_darwin_ax_trusted", fake_probe)

    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is True  # injection itself still proceeds
    assert inj.paste_chord() is True
    inj2 = PynputInjector(OutputConfig())  # second instance: still no spam
    assert inj2.backspace(1) is True

    assert probes == [1]
    assert len(notify_spy) == 1
    summary, body, urgency = notify_spy[0]
    assert "Accessibility" in summary
    assert "Privacy & Security" in body  # actionable fix path is included
    assert urgency == "critical"


def test_pynput_darwin_trusted_no_hint(monkeypatch, fake_pynput, notify_spy):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", False)
    monkeypatch.setattr(pyn_mod, "_darwin_ax_trusted", lambda: True)
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is True
    assert notify_spy == []


def test_pynput_darwin_unprobeable_stays_quiet(monkeypatch, fake_pynput, notify_spy):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", False)
    monkeypatch.setattr(pyn_mod, "_darwin_ax_trusted", lambda: None)
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is True
    assert notify_spy == []


def test_pynput_trust_probe_never_runs_off_darwin(monkeypatch, fake_pynput, notify_spy):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", False)
    probes: list[int] = []
    monkeypatch.setattr(pyn_mod, "_darwin_ax_trusted", lambda: probes.append(1) or False)
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is True
    assert probes == []
    assert notify_spy == []


def test_pynput_darwin_probe_crash_does_not_break_typing(monkeypatch, fake_pynput, notify_spy):
    # The probe is guarded: even if it explodes, injection succeeds and the
    # crash is never retried (flag is set before probing).
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pyn_mod, "_trust_probe_done", False)
    monkeypatch.setattr(
        pyn_mod, "_darwin_ax_trusted", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is True
    assert pyn_mod._trust_probe_done is True
    assert inj.type_text("again") is True
    assert notify_spy == []  # unknown trust state: no hint


def test_darwin_ax_trusted_via_fake_hiservices(monkeypatch):
    fake = types.ModuleType("HIServices")
    fake.AXIsProcessTrusted = lambda: False
    monkeypatch.setitem(sys.modules, "HIServices", fake)
    assert pyn_mod._darwin_ax_trusted() is False
    fake.AXIsProcessTrusted = lambda: 1  # truthy non-bool is normalized
    assert pyn_mod._darwin_ax_trusted() is True


def test_darwin_ax_trusted_unprobeable_returns_none(monkeypatch):
    import ctypes.util

    monkeypatch.setitem(sys.modules, "HIServices", None)  # forces ImportError
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    assert pyn_mod._darwin_ax_trusted() is None


def test_pynput_errors_return_false(monkeypatch):
    class AngryController:
        def type(self, s):
            raise RuntimeError("no accessibility permission")

        press = release = type

    kb_mod = types.ModuleType("pynput.keyboard")
    kb_mod.Controller = AngryController
    kb_mod.Key = FakeKey
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb_mod
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb_mod)
    inj = PynputInjector(OutputConfig())
    assert inj.type_text("hi") is False
    assert inj.backspace(1) is False
    assert inj.paste_chord() is False
    assert inj.tap_escape() is False


# =========================================================================
# windowinfo
# =========================================================================


def test_windowinfo_gnome(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")

    def run(args, **kwargs):
        assert args[0] == "gdbus"
        return SimpleNamespace(returncode=0, stdout="('org.gnome.Nautilus',)\n", stderr="")

    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.shutil, "which", which_factory())
    assert wi.focused_window_class() == "org.gnome.Nautilus"


def test_windowinfo_sway(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")
    tree = {
        "focused": False,
        "nodes": [
            {"focused": False, "nodes": [{"focused": True, "app_id": "kitty"}]},
        ],
        "floating_nodes": [],
    }

    def run(args, **kwargs):
        if args[0] == "gdbus":
            raise FileNotFoundError("gdbus")
        assert args == ["swaymsg", "-t", "get_tree"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(tree), stderr="")

    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.shutil, "which", which_factory("swaymsg"))
    assert wi.focused_window_class() == "kitty"


def test_windowinfo_sway_window_properties(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")
    tree = {
        "focused": False,
        "nodes": [],
        "floating_nodes": [
            {"focused": True, "app_id": None, "window_properties": {"class": "Alacritty"}}
        ],
    }

    def run(args, **kwargs):
        if args[0] == "gdbus":
            raise FileNotFoundError("gdbus")
        return SimpleNamespace(returncode=0, stdout=json.dumps(tree), stderr="")

    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.shutil, "which", which_factory("swaymsg"))
    assert wi.focused_window_class() == "Alacritty"


def test_windowinfo_hyprland(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")

    def run(args, **kwargs):
        if args[0] == "gdbus":
            raise FileNotFoundError("gdbus")
        assert args == ["hyprctl", "-j", "activewindow"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"class": "foot"}), stderr="")

    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.shutil, "which", which_factory("hyprctl"))
    assert wi.focused_window_class() == "foot"


def test_windowinfo_unknown(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")

    def run(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.shutil, "which", which_factory())
    assert wi.focused_window_class() is None


def test_windowinfo_darwin(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "darwin")

    def run(args, **kwargs):
        assert args[0] == "osascript"
        return SimpleNamespace(returncode=0, stdout="Safari\n", stderr="")

    monkeypatch.setattr(wi.subprocess, "run", run)
    assert wi.focused_window_class() == "Safari"


def test_windowinfo_win32_best_effort_none(monkeypatch):
    # On a POSIX test box ctypes has no windll: must degrade to None.
    monkeypatch.setattr(wi.sys, "platform", "win32")
    assert wi.focused_window_class() is None


def test_looks_like_terminal():
    terms = ["kitty", "org.gnome.Terminal", "cmd.exe", "terminal"]
    assert wi.looks_like_terminal("kitty", terms) is True
    assert wi.looks_like_terminal("Org.Gnome.Terminal", terms) is True
    assert wi.looks_like_terminal("FooTerminalBar", terms) is True
    assert wi.looks_like_terminal("CMD.EXE", terms) is True
    assert wi.looks_like_terminal("firefox", terms) is False
    assert wi.looks_like_terminal(None, terms) is False
    assert wi.looks_like_terminal("", terms) is False
    assert wi.looks_like_terminal("anything", []) is False


# =========================================================================
# get_injector selection
# =========================================================================


def _cfg(tmp_path: Path):
    return load_config(path=tmp_path / "missing.toml", env={})


def avail(value: bool):
    return classmethod(lambda cls: value)


def test_get_injector_linux_prefers_ydotool(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(YdotoolInjector, "available", avail(True))
    monkeypatch.setattr(XdotoolInjector, "available", avail(True))
    monkeypatch.setattr(PynputInjector, "available", avail(True))
    assert isinstance(get_injector(_cfg(tmp_path)), YdotoolInjector)


def test_get_injector_linux_falls_back_to_xdotool(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(YdotoolInjector, "available", avail(False))
    monkeypatch.setattr(XdotoolInjector, "available", avail(True))
    monkeypatch.setattr(PynputInjector, "available", avail(True))
    assert isinstance(get_injector(_cfg(tmp_path)), XdotoolInjector)


def test_get_injector_linux_falls_back_to_pynput(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(YdotoolInjector, "available", avail(False))
    monkeypatch.setattr(XdotoolInjector, "available", avail(False))
    monkeypatch.setattr(PynputInjector, "available", avail(True))
    assert isinstance(get_injector(_cfg(tmp_path)), PynputInjector)


def test_get_injector_darwin_only_pynput(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    # even if ydotool claims availability it must not be considered on mac
    monkeypatch.setattr(YdotoolInjector, "available", avail(True))
    monkeypatch.setattr(PynputInjector, "available", avail(True))
    assert isinstance(get_injector(_cfg(tmp_path)), PynputInjector)


def test_get_injector_linux_error_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(YdotoolInjector, "available", avail(False))
    monkeypatch.setattr(XdotoolInjector, "available", avail(False))
    monkeypatch.setattr(PynputInjector, "available", avail(False))
    with pytest.raises(RuntimeError) as exc:
        get_injector(_cfg(tmp_path))
    msg = str(exc.value)
    assert "ydotoold" in msg
    assert "input" in msg
    assert "setup-linux.sh" in msg


def test_get_injector_darwin_error_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(PynputInjector, "available", avail(False))
    with pytest.raises(RuntimeError) as exc:
        get_injector(_cfg(tmp_path))
    assert "Accessibility" in str(exc.value)


def test_get_injector_survives_probe_crash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        YdotoolInjector, "available", classmethod(lambda cls: 1 / 0)
    )
    monkeypatch.setattr(XdotoolInjector, "available", avail(True))
    assert isinstance(get_injector(_cfg(tmp_path)), XdotoolInjector)
    assert "availability check failed" in capsys.readouterr().err
