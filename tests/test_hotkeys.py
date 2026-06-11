"""Tests for the hotkey backends. Headless: fake evdev/pynput in sys.modules."""

from __future__ import annotations

import socket
import sys
import time
import types
from typing import Any

import pytest

from voicisst.config import Config
from voicisst.engine.base import EngineError
from voicisst.hotkeys import get_listener
from voicisst.hotkeys.evdev_listener import EvdevListener
from voicisst.hotkeys.pynput_listener import PynputListener, resolve_key_name

# ---------------------------------------------------------------------------
# Shared helpers


class Calls:
    """Records hotkey callback invocations."""

    def __init__(self) -> None:
        self.press = 0
        self.release = 0
        self.backspace = 0

    def on_press(self) -> None:
        self.press += 1

    def on_release(self) -> None:
        self.release += 1

    def on_backspace(self) -> None:
        self.backspace += 1


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Fake evdev

EV_KEY = 1
KEY_BACKSPACE = 14
KEY_COMPOSE = 127
KEY_MENU = 139


class FakeEvent:
    def __init__(self, etype: int, code: int, value: int):
        self.type = etype
        self.code = code
        self.value = value


class FakeDevice:
    def __init__(
        self,
        path: str,
        name: str,
        key_caps: list[int],
        fd: int = -1,
        permission_error: bool = False,
        sock: socket.socket | None = None,
    ):
        self.path = path
        self.name = name
        self._caps = list(key_caps)
        self._sock = sock
        self.fd = sock.fileno() if sock is not None else fd
        self.permission_error = permission_error
        self.closed = False
        self.events: list[FakeEvent] = []

    def capabilities(self) -> dict[int, list[int]]:
        return {EV_KEY: list(self._caps)}

    def read(self):
        if self._sock is not None:
            self._sock.recv(4096)  # drain the wakeup byte(s)
        if not self.events:
            raise BlockingIOError
        events, self.events = self.events, []
        return iter(events)

    def close(self) -> None:
        self.closed = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


def make_fake_evdev(devices: list[FakeDevice]) -> types.ModuleType:
    mod = types.ModuleType("evdev")
    ecodes = types.ModuleType("evdev.ecodes")
    ecodes.EV_KEY = EV_KEY
    ecodes.KEY_BACKSPACE = KEY_BACKSPACE
    ecodes.ecodes = {
        "KEY_MENU": KEY_MENU,
        "KEY_COMPOSE": KEY_COMPOSE,
        "KEY_BACKSPACE": KEY_BACKSPACE,
        "KEY_A": 30,
    }
    by_path = {d.path: d for d in devices}

    def input_device(path: str) -> FakeDevice:
        dev = by_path[path]
        if dev.permission_error:
            raise PermissionError(13, "Permission denied", path)
        return dev

    mod.ecodes = ecodes
    mod.list_devices = lambda: [d.path for d in devices]
    mod.InputDevice = input_device
    return mod


def install_fake_evdev(monkeypatch, devices: list[FakeDevice]) -> types.ModuleType:
    mod = make_fake_evdev(devices)
    monkeypatch.setitem(sys.modules, "evdev", mod)
    monkeypatch.setitem(sys.modules, "evdev.ecodes", mod.ecodes)
    return mod


# ---------------------------------------------------------------------------
# evdev: device discovery


def test_evdev_discovery_filters_devices(monkeypatch, capsys):
    keyboard = FakeDevice("/dev/input/event0", "AT Translated Keyboard", [KEY_MENU, 30])
    ydotool = FakeDevice("/dev/input/event1", "ydotoold virtual device", [KEY_MENU])
    mouse = FakeDevice("/dev/input/event2", "Some Mouse", [0x110])
    denied = FakeDevice("/dev/input/event3", "Locked Keyboard", [KEY_MENU], permission_error=True)
    install_fake_evdev(monkeypatch, [keyboard, ydotool, mouse, denied])

    calls = Calls()
    listener = EvdevListener(
        ["KEY_COMPOSE", "KEY_MENU"], calls.on_press, calls.on_release, calls.on_backspace
    )
    devices, wanted = listener._find_devices()

    assert devices == [keyboard]
    assert wanted == {KEY_COMPOSE, KEY_MENU}
    assert ydotool.closed  # our own virtual keyboard must never be listened to
    assert mouse.closed
    err = capsys.readouterr().err
    assert "ydotool" in err.lower()
    assert "event3" in err  # permission-denied path reported, not fatal


def test_evdev_discovery_warns_on_unknown_key_name(monkeypatch, capsys):
    keyboard = FakeDevice("/dev/input/event0", "kbd", [KEY_MENU])
    install_fake_evdev(monkeypatch, [keyboard])
    calls = Calls()
    listener = EvdevListener(["KEY_MENU", "KEY_BOGUS"], calls.on_press, calls.on_release)
    devices, wanted = listener._find_devices()
    assert wanted == {KEY_MENU}
    assert devices == [keyboard]
    assert "KEY_BOGUS" in capsys.readouterr().err


def test_evdev_discovery_all_unknown_keys_raises(monkeypatch):
    install_fake_evdev(monkeypatch, [FakeDevice("/dev/input/event0", "kbd", [KEY_MENU])])
    calls = Calls()
    listener = EvdevListener(["KEY_BOGUS"], calls.on_press, calls.on_release)
    with pytest.raises(EngineError) as exc:
        listener._find_devices()
    assert "KEY_MENU" in exc.value.hint  # suggests valid names


def test_evdev_start_readable_devices_without_key_hints_key_config(monkeypatch):
    """Permissions were fine, the configured key just doesn't exist on any
    device: the hint must suggest configuring a key the user has, NOT the
    input group (which is already working)."""
    mouse = FakeDevice("/dev/input/event0", "Some Mouse", [0x110])
    install_fake_evdev(monkeypatch, [mouse])
    calls = Calls()
    listener = EvdevListener(["KEY_MENU"], calls.on_press, calls.on_release)
    with pytest.raises(EngineError) as exc:
        listener.start()
    assert "readable" in str(exc.value)
    assert "hotkey.keys" in exc.value.hint
    assert "KEY_RIGHTALT" in exc.value.hint  # example key names
    assert "evdev.evtest" in exc.value.hint  # how to list what they have
    assert "usermod" not in exc.value.hint  # input group is NOT the problem


def test_evdev_start_unreadable_devices_hints_input_group(monkeypatch):
    denied = FakeDevice(
        "/dev/input/event0", "Locked Keyboard", [KEY_MENU], permission_error=True
    )
    install_fake_evdev(monkeypatch, [denied])
    calls = Calls()
    listener = EvdevListener(["KEY_MENU"], calls.on_press, calls.on_release)
    with pytest.raises(EngineError) as exc:
        listener.start()
    assert "usermod -aG input" in exc.value.hint
    assert "evdev.evtest" not in exc.value.hint  # listing keys won't help yet


def test_evdev_start_no_devices_at_all_hints_input_group(monkeypatch):
    install_fake_evdev(monkeypatch, [])
    calls = Calls()
    listener = EvdevListener(["KEY_MENU"], calls.on_press, calls.on_release)
    with pytest.raises(EngineError) as exc:
        listener.start()
    assert "usermod -aG input" in exc.value.hint


# ---------------------------------------------------------------------------
# evdev: event dispatch (pure _handle_events core)


def make_evdev_listener(calls: Calls, backspace: bool = True) -> EvdevListener:
    listener = EvdevListener(
        ["KEY_MENU"],
        calls.on_press,
        calls.on_release,
        calls.on_backspace if backspace else None,
    )
    listener._wanted_codes = {KEY_MENU}
    return listener


def test_evdev_press_release_and_autorepeat():
    calls = Calls()
    listener = make_evdev_listener(calls)

    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 1)])
    assert calls.press == 1
    assert listener._holder_fd == 5

    # autorepeat (value == 2) must be ignored
    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 2)] * 3)
    assert calls.press == 1

    # non-key events are ignored
    listener._handle_events(5, [FakeEvent(4, KEY_MENU, 1)])
    assert calls.press == 1

    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 0)])
    assert calls.release == 1
    assert listener._holder_fd is None


def test_evdev_holder_fd_arbitration_first_device_wins():
    calls = Calls()
    listener = make_evdev_listener(calls)

    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 1)])  # device A holds
    listener._handle_events(6, [FakeEvent(EV_KEY, KEY_MENU, 1)])  # device B ignored
    assert calls.press == 1

    listener._handle_events(6, [FakeEvent(EV_KEY, KEY_MENU, 0)])  # B's key-up ignored
    assert calls.release == 0

    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 0)])  # A releases
    assert calls.release == 1

    listener._handle_events(6, [FakeEvent(EV_KEY, KEY_MENU, 1)])  # B may hold now
    assert calls.press == 2


def test_evdev_backspace_always_forwarded():
    calls = Calls()
    listener = make_evdev_listener(calls)

    # forwarded even with no hotkey held / no recording in progress
    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_BACKSPACE, 1)])
    assert calls.backspace == 1
    # autorepeat and key-up of backspace are not presses
    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_BACKSPACE, 2)])
    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_BACKSPACE, 0)])
    assert calls.backspace == 1
    # backspace never disturbs holder arbitration
    assert listener._holder_fd is None
    assert calls.press == 0


def test_evdev_backspace_without_callback_is_safe():
    calls = Calls()
    listener = make_evdev_listener(calls, backspace=False)
    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_BACKSPACE, 1)])  # no crash
    assert calls.backspace == 0


def test_evdev_dead_device_removal_synthesizes_release(capsys):
    calls = Calls()
    listener = make_evdev_listener(calls)
    dev_a = FakeDevice("/dev/input/event0", "kbd A", [KEY_MENU], fd=5)
    dev_b = FakeDevice("/dev/input/event1", "kbd B", [KEY_MENU], fd=6)
    listener._fd_to_dev = {5: dev_a, 6: dev_b}

    listener._handle_events(5, [FakeEvent(EV_KEY, KEY_MENU, 1)])  # A holds
    listener._drop_device(6, OSError("gone"))  # non-holder dies: no release
    assert calls.release == 0
    assert dev_b.closed
    assert 6 not in listener._fd_to_dev

    listener._drop_device(5, OSError("gone"))  # holder dies: synthetic release
    assert calls.release == 1
    assert dev_a.closed
    assert listener._fd_to_dev == {}
    err = capsys.readouterr().err
    assert "removing" in err
    assert "no usable keyboards left" in err


def test_evdev_loop_end_to_end(monkeypatch):
    """Full thread: select() wakeup via socketpair, events dispatched, stop joins."""
    wake_tx, wake_rx = socket.socketpair()
    dev = FakeDevice("/dev/input/event0", "kbd", [KEY_MENU], sock=wake_rx)
    install_fake_evdev(monkeypatch, [dev])

    calls = Calls()
    listener = EvdevListener(["KEY_MENU"], calls.on_press, calls.on_release, calls.on_backspace)
    listener.start()
    try:
        dev.events.append(FakeEvent(EV_KEY, KEY_MENU, 1))
        wake_tx.sendall(b"x")
        assert wait_for(lambda: calls.press == 1)

        dev.events.append(FakeEvent(EV_KEY, KEY_MENU, 0))
        wake_tx.sendall(b"x")
        assert wait_for(lambda: calls.release == 1)
    finally:
        listener.stop()
        wake_tx.close()
    assert dev.closed
    listener.stop()  # idempotent


def test_evdev_available(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    install_fake_evdev(monkeypatch, [FakeDevice("/dev/input/event0", "kbd", [KEY_MENU])])
    assert EvdevListener.available() is True

    install_fake_evdev(
        monkeypatch,
        [FakeDevice("/dev/input/event0", "kbd", [KEY_MENU], permission_error=True)],
    )
    assert EvdevListener.available() is False  # PermissionError -> False

    install_fake_evdev(monkeypatch, [])
    assert EvdevListener.available() is False  # no devices at all

    monkeypatch.setattr(sys, "platform", "darwin")
    assert EvdevListener.available() is False


# ---------------------------------------------------------------------------
# Fake pynput


class FakeSpecialKey:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Key.{self.name}"


def make_fake_pynput() -> types.ModuleType:
    pynput = types.ModuleType("pynput")
    keyboard = types.ModuleType("pynput.keyboard")

    class Key:
        pass

    special = [
        "alt_l", "alt_r", "alt_gr", "ctrl_l", "ctrl_r", "shift_l", "shift_r",
        "cmd", "cmd_r", "menu", "backspace", "esc", "tab", "space", "enter",
        "caps_lock", "insert", "delete", "home", "end", "page_up", "page_down",
    ] + [f"f{i}" for i in range(1, 21)]  # real pynput stops at f20
    for name in special:
        setattr(Key, name, FakeSpecialKey(name))

    class KeyCode:
        def __init__(self, char: str | None = None):
            self.char = char

        @classmethod
        def from_char(cls, char: str) -> KeyCode:
            return cls(char=char)

        def __eq__(self, other: object) -> bool:
            return isinstance(other, KeyCode) and other.char == self.char

        def __hash__(self) -> int:
            return hash(self.char)

        def __repr__(self) -> str:  # pragma: no cover - debugging aid
            return f"KeyCode({self.char!r})"

    listeners: list[Any] = []

    class Listener:
        def __init__(self, on_press=None, on_release=None, suppress=False):
            self.on_press = on_press
            self.on_release = on_release
            self.suppress = suppress
            self.started = False
            self.stopped = False
            listeners.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def join(self, timeout=None) -> None:
            pass

        def is_alive(self) -> bool:
            return self.started and not self.stopped

    keyboard.Key = Key
    keyboard.KeyCode = KeyCode
    keyboard.Listener = Listener
    pynput.keyboard = keyboard
    pynput._listeners = listeners
    return pynput


@pytest.fixture
def fake_pynput(monkeypatch) -> types.ModuleType:
    mod = make_fake_pynput()
    monkeypatch.setitem(sys.modules, "pynput", mod)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", mod.keyboard)
    return mod


# ---------------------------------------------------------------------------
# pynput: key-name resolution


def test_pynput_key_name_resolution_table(fake_pynput):
    Key = fake_pynput.keyboard.Key
    KeyCode = fake_pynput.keyboard.KeyCode
    table = [
        # pynput special names pass straight through
        ("alt_r", Key.alt_r),
        ("ctrl_r", Key.ctrl_r),
        ("menu", Key.menu),
        ("f9", Key.f9),
        ("F9", Key.f9),  # case-insensitive
        # single characters
        ("a", KeyCode.from_char("a")),
        ("X", KeyCode.from_char("x")),  # normalized to lowercase
        # evdev-style names map to the pynput equivalent
        ("KEY_MENU", Key.menu),
        ("KEY_COMPOSE", Key.menu),
        ("KEY_RIGHTALT", Key.alt_r),
        ("KEY_RIGHTCTRL", Key.ctrl_r),
        ("KEY_F1", Key.f1),
        ("KEY_F9", Key.f9),
        ("key_menu", Key.menu),  # case-insensitive evdev names too
        ("KEY_A", KeyCode.from_char("a")),
        ("KEY_1", KeyCode.from_char("1")),
        # unmappable
        ("KEY_BANANAPHONE", None),
        ("KEY_F22", None),  # pynput has no f22 attribute
        ("totally_bogus", None),
        ("", None),
    ]
    for name, expected in table:
        assert resolve_key_name(name) == expected, name


def test_pynput_resolve_warns_and_keeps_valid_keys(fake_pynput, capsys, monkeypatch):
    # Pin to linux: on real win32 resolution adds the Key.alt_gr companion.
    monkeypatch.setattr(sys, "platform", "linux")
    calls = Calls()
    listener = PynputListener(
        ["KEY_BANANAPHONE", "alt_r"], calls.on_press, calls.on_release
    )
    resolved = listener._resolve_keys()
    assert resolved == [fake_pynput.keyboard.Key.alt_r]
    err = capsys.readouterr().err
    assert "KEY_BANANAPHONE" in err
    assert "alt_r" in err  # suggestion in the warning


def test_pynput_resolve_raises_only_when_nothing_maps(fake_pynput):
    calls = Calls()
    listener = PynputListener(["KEY_BANANAPHONE"], calls.on_press, calls.on_release)
    with pytest.raises(EngineError) as exc:
        listener._resolve_keys()
    assert "hotkey.keys" in exc.value.hint


# ---------------------------------------------------------------------------
# pynput: dispatch + autorepeat suppression


def start_pynput(fake_pynput, keys: list[str], backspace: bool = True):
    calls = Calls()
    listener = PynputListener(
        keys,
        calls.on_press,
        calls.on_release,
        calls.on_backspace if backspace else None,
    )
    listener.start()
    fake = fake_pynput._listeners[-1]
    assert fake.started
    assert fake.suppress is False  # never swallow keys
    return listener, fake, calls


def test_pynput_autorepeat_suppressed_via_held_set(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    Key = fake_pynput.keyboard.Key

    fake.on_press(Key.alt_r)
    fake.on_press(Key.alt_r)  # OS autorepeat: repeated on_press, no release
    fake.on_press(Key.alt_r)
    assert calls.press == 1

    fake.on_release(Key.alt_r)
    assert calls.release == 1

    fake.on_press(Key.alt_r)  # a new physical press fires again
    assert calls.press == 2
    listener.stop()


def test_pynput_backspace_always_forwarded_and_repeat_suppressed(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    Key = fake_pynput.keyboard.Key

    fake.on_press(Key.backspace)
    fake.on_press(Key.backspace)  # autorepeat
    assert calls.backspace == 1
    fake.on_release(Key.backspace)
    fake.on_press(Key.backspace)
    assert calls.backspace == 2
    assert calls.press == 0  # backspace is not a hotkey
    listener.stop()


def test_pynput_backspace_without_callback_is_safe(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"], backspace=False)
    fake.on_press(fake_pynput.keyboard.Key.backspace)  # no crash
    assert calls.backspace == 0
    listener.stop()


def test_pynput_char_hotkey_matches_shifted_char(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["x"])
    KeyCode = fake_pynput.keyboard.KeyCode
    fake.on_press(KeyCode.from_char("X"))  # shifted variant
    assert calls.press == 1
    fake.on_release(KeyCode.from_char("x"))
    assert calls.release == 1
    fake.on_press(None)  # pynput reports unknown keys as None: ignored
    fake.on_release(None)
    assert calls.press == 1
    listener.stop()


def test_pynput_holder_arbitration_between_two_hotkeys(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r", "ctrl_r"])
    Key = fake_pynput.keyboard.Key

    fake.on_press(Key.alt_r)  # alt_r holds
    fake.on_press(Key.ctrl_r)  # second hotkey ignored while held
    assert calls.press == 1
    fake.on_release(Key.ctrl_r)  # non-holder release ignored
    assert calls.release == 0
    fake.on_release(Key.alt_r)
    assert calls.release == 1
    fake.on_press(Key.ctrl_r)  # free again
    assert calls.press == 2
    listener.stop()


def test_pynput_other_keys_ignored_and_stop_idempotent(fake_pynput):
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    Key = fake_pynput.keyboard.Key
    fake.on_press(Key.esc)
    fake.on_release(Key.esc)
    assert (calls.press, calls.release, calls.backspace) == (0, 0, 0)
    listener.stop()
    assert fake.stopped
    listener.stop()  # idempotent


# ---------------------------------------------------------------------------
# pynput: injected (synthetic) events must be ignored


def test_pynput_injected_events_ignored(fake_pynput):
    """pynput >= 1.8 calls callbacks as (key, injected). Voicisst's own synthetic
    keystrokes — e.g. its backspaces during streaming polish — arrive with
    injected=True and must not fire callbacks, or the injected backspaces
    would self-cancel the polish."""
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    Key = fake_pynput.keyboard.Key

    fake.on_press(Key.backspace, True)  # our own backspace during polish
    fake.on_press(Key.alt_r, True)  # injected hotkey press
    fake.on_release(Key.alt_r, True)
    assert (calls.press, calls.release, calls.backspace) == (0, 0, 0)
    assert listener._held == []  # injected events must not poison held-state

    fake.on_press(Key.alt_r)  # physical, older-pynput shape: key only
    assert calls.press == 1
    fake.on_release(Key.alt_r, False)  # physical with explicit flag
    assert calls.release == 1
    fake.on_press(Key.backspace, False)
    assert calls.backspace == 1
    listener.stop()


def _load_real_pynput_util():
    """Load pynput._util straight from its file, bypassing pynput/__init__.py
    (which probes display backends and fails on headless CI)."""
    import importlib.util
    from importlib.machinery import PathFinder
    from pathlib import Path

    # PathFinder searches sys.path directly: it neither executes the package
    # nor is fooled by the fake "pynput" the fixtures put in sys.modules.
    spec = PathFinder.find_spec("pynput")
    if spec is None or spec.origin is None:
        pytest.skip("pynput is not installed")
    util_path = Path(spec.origin).parent / "_util" / "__init__.py"
    if not util_path.exists():
        pytest.skip("pynput._util not found")
    mod_spec = importlib.util.spec_from_file_location("_pynput_util_for_test", util_path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return mod


def test_pynput_handlers_receive_injected_flag_from_real_wrap(fake_pynput):
    """Verify against the INSTALLED pynput that Listener._wrap(f, 2) passes
    our handlers through unwrapped, so the (key, injected) positional call
    from the darwin/win32/xorg backends reaches them. A one-parameter
    handler would be silently truncated to (key,) — the original bug."""
    util = _load_real_pynput_util()
    wrap = util.AbstractListener._wrap  # plain function: (self, f, args)
    calls = Calls()
    listener = PynputListener(["alt_r"], calls.on_press, calls.on_release)
    listener.start()
    dummy_self = object()  # _wrap never touches self
    for handler in (listener._handle_press, listener._handle_release):
        assert wrap(dummy_self, handler, 2) is handler, (
            f"{handler.__name__} must accept (key, injected) or pynput "
            "drops the injected flag"
        )
    # the one-arg shape (the buggy signature) really is truncated by _wrap
    one_arg = lambda key: None  # noqa: E731
    assert wrap(dummy_self, one_arg, 2) is not one_arg
    listener.stop()


# ---------------------------------------------------------------------------
# pynput: Windows reports right Alt as alt_gr (vk 0xA5)


def test_pynput_win32_resolution_adds_alt_gr_companion(fake_pynput, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    Key = fake_pynput.keyboard.Key
    calls = Calls()

    listener = PynputListener(["alt_r"], calls.on_press, calls.on_release)
    assert listener._resolve_keys() == [Key.alt_r, Key.alt_gr]

    # evdev-style config keeps working
    listener = PynputListener(["KEY_RIGHTALT"], calls.on_press, calls.on_release)
    assert listener._resolve_keys() == [Key.alt_r, Key.alt_gr]

    # vice versa: a configured alt_gr also watches alt_r
    listener = PynputListener(["alt_gr"], calls.on_press, calls.on_release)
    assert listener._resolve_keys() == [Key.alt_gr, Key.alt_r]

    # both configured: no duplicates
    listener = PynputListener(["alt_r", "alt_gr"], calls.on_press, calls.on_release)
    assert listener._resolve_keys() == [Key.alt_r, Key.alt_gr]

    # unrelated keys get no companion
    listener = PynputListener(["f9"], calls.on_press, calls.on_release)
    assert listener._resolve_keys() == [Key.f9]


def test_pynput_non_win32_resolution_has_no_alt_gr_companion(fake_pynput, monkeypatch):
    Key = fake_pynput.keyboard.Key
    calls = Calls()
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        listener = PynputListener(["alt_r"], calls.on_press, calls.on_release)
        assert listener._resolve_keys() == [Key.alt_r]


def test_pynput_win32_right_alt_arriving_as_alt_gr_fires_hotkey(fake_pynput, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    Key = fake_pynput.keyboard.Key
    fake.on_press(Key.alt_gr)  # what Windows actually delivers for right Alt
    assert calls.press == 1
    fake.on_release(Key.alt_gr)
    assert calls.release == 1
    listener.stop()


# ---------------------------------------------------------------------------
# pynput: macOS Input-Monitoring denial must not be silent


def patch_notify(monkeypatch) -> list[tuple]:
    import voicisst.notify as notify_mod

    notifications: list[tuple] = []
    monkeypatch.setattr(
        notify_mod,
        "notify",
        lambda summary, body="", urgency="low", **kw: notifications.append(
            (summary, body, urgency)
        ),
    )
    return notifications


def test_pynput_darwin_untrusted_warns_and_notifies_critical(
    fake_pynput, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "platform", "darwin")
    Listener = fake_pynput.keyboard.Listener
    monkeypatch.setattr(Listener, "IS_TRUSTED", False, raising=False)
    monkeypatch.setattr(Listener, "_ready", True, raising=False)
    notifications = patch_notify(monkeypatch)

    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])  # starts fine
    err = capsys.readouterr().err
    assert "Input Monitoring" in err
    assert "System Settings" in err and "Privacy & Security" in err
    assert notifications == [
        (
            "voicisst hotkeys: macOS denied Input Monitoring",
            "grant your terminal/app permission in System Settings -> "
            "Privacy & Security -> Input Monitoring, then restart voicisst",
            "critical",
        )
    ]
    listener.stop()


def test_pynput_darwin_untrusted_dead_thread_before_ready_still_warns(
    fake_pynput, monkeypatch, capsys
):
    """The darwin listener thread can die before marking itself ready (event
    tap creation failed); the check must not hang and must still warn."""
    monkeypatch.setattr(sys, "platform", "darwin")
    Listener = fake_pynput.keyboard.Listener
    monkeypatch.setattr(Listener, "IS_TRUSTED", False, raising=False)
    monkeypatch.setattr(Listener, "_ready", False, raising=False)
    monkeypatch.setattr(Listener, "is_alive", lambda self: False)
    notifications = patch_notify(monkeypatch)

    start = time.monotonic()
    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    assert time.monotonic() - start < 1.0  # no 2s deadline wait
    assert "Input Monitoring" in capsys.readouterr().err
    assert len(notifications) == 1
    listener.stop()


def test_pynput_darwin_trusted_no_warning(fake_pynput, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    Listener = fake_pynput.keyboard.Listener
    monkeypatch.setattr(Listener, "IS_TRUSTED", True, raising=False)
    monkeypatch.setattr(Listener, "_ready", True, raising=False)
    notifications = patch_notify(monkeypatch)

    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    assert "Input Monitoring" not in capsys.readouterr().err
    assert notifications == []
    listener.stop()


def test_pynput_trust_check_skipped_off_darwin(fake_pynput, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    Listener = fake_pynput.keyboard.Listener
    monkeypatch.setattr(Listener, "IS_TRUSTED", False, raising=False)
    notifications = patch_notify(monkeypatch)

    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])
    assert "Input Monitoring" not in capsys.readouterr().err
    assert notifications == []
    listener.stop()


def test_pynput_darwin_untrusted_notify_failure_does_not_break_start(
    fake_pynput, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "platform", "darwin")
    Listener = fake_pynput.keyboard.Listener
    monkeypatch.setattr(Listener, "IS_TRUSTED", False, raising=False)
    monkeypatch.setattr(Listener, "_ready", True, raising=False)
    import voicisst.notify as notify_mod

    def boom(*a, **k):
        raise RuntimeError("no notifier")

    monkeypatch.setattr(notify_mod, "notify", boom)

    listener, fake, calls = start_pynput(fake_pynput, ["alt_r"])  # must not raise
    err = capsys.readouterr().err
    assert "Input Monitoring" in err  # stderr warning still happened
    assert "notification failed" in err
    fake.on_press(fake_pynput.keyboard.Key.alt_r)  # listener still works
    assert calls.press == 1
    listener.stop()


def test_pynput_available(monkeypatch, fake_pynput):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert PynputListener.available() is False  # pure Wayland / headless

    monkeypatch.setenv("DISPLAY", ":0")
    assert PynputListener.available() is True  # X11 (or XWayland)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert PynputListener.available() is True

    monkeypatch.setattr(sys, "platform", "win32")
    assert PynputListener.available() is True

    monkeypatch.setitem(sys.modules, "pynput", None)  # import fails
    assert PynputListener.available() is False


# ---------------------------------------------------------------------------
# get_listener backend selection


def make_cfg(backend: str) -> Config:
    cfg = Config()
    cfg.hotkey.backend = backend
    cfg.hotkey.keys = ["KEY_MENU"]
    return cfg


def force_available(monkeypatch, evdev: bool, pynput: bool) -> None:
    monkeypatch.setattr(EvdevListener, "available", classmethod(lambda cls: evdev))
    monkeypatch.setattr(PynputListener, "available", classmethod(lambda cls: pynput))


def test_get_listener_auto_linux_prefers_evdev(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    force_available(monkeypatch, evdev=True, pynput=True)
    calls = Calls()
    listener = get_listener(make_cfg("auto"), calls.on_press, calls.on_release, calls.on_backspace)
    assert isinstance(listener, EvdevListener)
    assert listener.keys == ["KEY_MENU"]
    assert listener.on_press == calls.on_press
    assert listener.on_backspace == calls.on_backspace


def test_get_listener_auto_linux_falls_back_to_pynput(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    force_available(monkeypatch, evdev=False, pynput=True)
    calls = Calls()
    listener = get_listener(make_cfg("auto"), calls.on_press, calls.on_release)
    assert isinstance(listener, PynputListener)


def test_get_listener_auto_linux_nothing_available(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    force_available(monkeypatch, evdev=False, pynput=False)
    calls = Calls()
    with pytest.raises(EngineError) as exc:
        get_listener(make_cfg("auto"), calls.on_press, calls.on_release)
    assert "usermod -aG input" in exc.value.hint


def test_get_listener_auto_mac_uses_pynput(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    force_available(monkeypatch, evdev=False, pynput=True)
    calls = Calls()
    listener = get_listener(make_cfg("auto"), calls.on_press, calls.on_release)
    assert isinstance(listener, PynputListener)


def test_get_listener_explicit_evdev_unavailable_hints_input_group(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    force_available(monkeypatch, evdev=False, pynput=True)
    calls = Calls()
    with pytest.raises(EngineError) as exc:
        get_listener(make_cfg("evdev"), calls.on_press, calls.on_release)
    assert "sudo usermod -aG input $USER" in exc.value.hint
    assert "setup-linux.sh" in exc.value.hint


def test_get_listener_explicit_evdev_on_mac_rejected(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    force_available(monkeypatch, evdev=False, pynput=True)
    calls = Calls()
    with pytest.raises(EngineError) as exc:
        get_listener(make_cfg("evdev"), calls.on_press, calls.on_release)
    assert "Linux" in str(exc.value)


def test_get_listener_explicit_pynput_honored_and_failure_is_helpful(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    force_available(monkeypatch, evdev=True, pynput=True)
    calls = Calls()
    listener = get_listener(make_cfg("pynput"), calls.on_press, calls.on_release)
    assert isinstance(listener, PynputListener)  # explicit choice beats evdev

    force_available(monkeypatch, evdev=True, pynput=False)
    with pytest.raises(EngineError) as exc:
        get_listener(make_cfg("pynput"), calls.on_press, calls.on_release)
    assert "evdev" in exc.value.hint  # points at the Wayland-capable backend


def test_get_listener_unknown_backend(monkeypatch):
    force_available(monkeypatch, evdev=True, pynput=True)
    calls = Calls()
    with pytest.raises(EngineError) as exc:
        get_listener(make_cfg("banana"), calls.on_press, calls.on_release)
    assert "banana" in str(exc.value)
    assert "auto" in exc.value.hint
