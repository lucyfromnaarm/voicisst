"""evdev-based global hotkey listener (Linux, Wayland and X11).

Reads raw kernel input events from /dev/input — no display server needed,
which makes this the only reliable global-hotkey path on Wayland. Requires
read access to /dev/input/event* (the 'input' group on most distros).
"""

from __future__ import annotations

import select
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from ..engine.base import EngineError
from .base import HotkeyListener

INPUT_GROUP_HINT = (
    "add yourself to the 'input' group: sudo usermod -aG input $USER, "
    "then re-login; or run scripts/setup-linux.sh"
)


class EvdevListener(HotkeyListener):
    """Multi-keyboard hotkey listener with holder-fd arbitration.

    Only the *first* device whose hotkey went down is honored until that
    same device's key comes back up — prevents multi-keyboard races.
    Autorepeat (event value == 2) is ignored. Backspace presses are ALWAYS
    forwarded to `on_backspace`; the app decides whether they matter.
    """

    name = "evdev"

    def __init__(
        self,
        keys: Sequence[str],
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_backspace: Callable[[], None] | None = None,
    ):
        super().__init__(keys, on_press, on_release, on_backspace)
        self._fd_to_dev: dict[int, Any] = {}
        self._wanted_codes: set[int] = set()
        self._holder_fd: int | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Did the last _find_devices() manage to open any /dev/input device?
        # Distinguishes "no permission" from "no keyboard has the key".
        self._saw_readable_device = False
        # Stable kernel constants; refreshed from evdev.ecodes in
        # _find_devices() so a (test-)injected evdev module wins.
        self._ev_key: int = 1  # ecodes.EV_KEY
        self._key_backspace: int = 14  # ecodes.KEY_BACKSPACE

    # -- availability -------------------------------------------------------

    @classmethod
    def available(cls) -> bool:
        """Linux + evdev importable + at least one readable input device."""
        if sys.platform != "linux":
            return False
        try:
            import evdev
        except ImportError:
            return False
        try:
            paths = evdev.list_devices()
        except OSError:
            return False
        for path in paths:
            try:
                dev = evdev.InputDevice(path)
            except (PermissionError, OSError):
                continue
            try:
                dev.close()
            except Exception:
                pass
            return True
        return False

    # -- device discovery (port of prototype find_hotkey_devices) -----------

    def _find_devices(self) -> tuple[list[Any], set[int]]:
        """Return (devices exposing a wanted key, set of wanted keycodes)."""
        import evdev
        from evdev import ecodes

        self._ev_key = ecodes.EV_KEY
        self._key_backspace = ecodes.KEY_BACKSPACE

        wanted_codes: set[int] = set()
        for name in self.keys:
            code = ecodes.ecodes.get(name)
            if code is None:
                print(
                    f"voicisst hotkeys: unknown evdev key name {name!r} — skipping "
                    "(use names like KEY_MENU or KEY_RIGHTALT)",
                    file=sys.stderr,
                )
                continue
            wanted_codes.add(code)
        if not wanted_codes:
            raise EngineError(
                f"no valid evdev key names in hotkey.keys = {self.keys}",
                hint='use evdev names like "KEY_MENU", "KEY_COMPOSE" or "KEY_RIGHTALT" '
                "(list them with: python -m evdev.evtest)",
            )

        devices: list[Any] = []
        self._saw_readable_device = False
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except (PermissionError, OSError) as e:
                print(f"voicisst hotkeys: skip {path}: {e}", file=sys.stderr)
                continue
            self._saw_readable_device = True
            # Skip ydotool's own virtual keyboard. We use it to type and
            # backspace, so listening on it would feedback-loop our generated
            # Backspace events into the polish-cancel path.
            name = (dev.name or "").lower()
            if "ydotool" in name:
                print(
                    f"voicisst hotkeys: skip {path}: {dev.name} (our own virtual keyboard)",
                    file=sys.stderr,
                )
                self._close_quietly(dev)
                continue
            caps = set(dev.capabilities().get(ecodes.EV_KEY, []))
            if caps & wanted_codes:
                devices.append(dev)
            else:
                self._close_quietly(dev)
        return devices, wanted_codes

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        devices, wanted_codes = self._find_devices()
        if not devices:
            if self._saw_readable_device:
                # Permissions are fine — the user's keyboards just do not
                # have any of the configured keys. The input-group hint
                # would send them down the wrong path.
                raise EngineError(
                    f"input devices are readable, but none exposes any of {self.keys}",
                    hint='set hotkey.keys to a key your keyboard has '
                    '(e.g. "KEY_RIGHTALT", "KEY_F9"); '
                    "find names with: python -m evdev.evtest",
                )
            raise EngineError(
                f"none of /dev/input/event* is readable (cannot watch {self.keys})",
                hint=INPUT_GROUP_HINT,
            )
        self._wanted_codes = wanted_codes
        self._fd_to_dev = {dev.fd: dev for dev in devices}
        self._holder_fd = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="voicisst-evdev-hotkeys", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        for fd in list(self._fd_to_dev):
            dev = self._fd_to_dev.pop(fd, None)
            if dev is not None:
                self._close_quietly(dev)
        self._holder_fd = None

    # -- event loop -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set() and self._fd_to_dev:
            try:
                readable, _, _ = select.select(list(self._fd_to_dev), [], [], 0.2)
            except (OSError, ValueError):
                if self._stop_event.is_set():
                    return
                self._prune_dead_fds()
                continue
            for fd in readable:
                dev = self._fd_to_dev.get(fd)
                if dev is None:
                    continue
                try:
                    events = list(dev.read())
                except BlockingIOError:
                    continue
                except OSError as e:
                    self._drop_device(fd, e)
                    continue
                self._handle_events(fd, events)

    def _handle_events(self, fd: int, events: Iterable[Any]) -> None:
        """Process one batch of input events from device `fd`.

        Pure state-machine core (no I/O): holder-fd arbitration, autorepeat
        suppression, backspace forwarding. Factored out for testability.
        """
        for event in events:
            if event.type != self._ev_key:
                continue
            # Backspace press anywhere is always forwarded; the dictation
            # app decides whether it matters (polish-cancel window).
            if event.code == self._key_backspace:
                if event.value == 1:
                    self._safe(self.on_backspace)
                continue
            if event.code not in self._wanted_codes:
                continue
            if event.value == 1:  # key down
                if self._holder_fd is None:
                    self._holder_fd = fd
                    self._safe(self.on_press)
            elif event.value == 0:  # key up
                if self._holder_fd == fd:
                    self._holder_fd = None
                    self._safe(self.on_release)
            # value == 2 is autorepeat — ignore.

    def _drop_device(self, fd: int, error: Exception | None = None) -> None:
        """Remove a dead device. If it held the hotkey, synthesize a release."""
        dev = self._fd_to_dev.pop(fd, None)
        if dev is not None:
            path = getattr(dev, "path", fd)
            print(f"voicisst hotkeys: device {path} dead ({error}); removing", file=sys.stderr)
            self._close_quietly(dev)
        if self._holder_fd == fd:
            # The held key can never come up now — report a release so the
            # app stops recording instead of hanging in "listening".
            self._holder_fd = None
            self._safe(self.on_release)
        if not self._fd_to_dev:
            print(
                "voicisst hotkeys: no usable keyboards left — hotkeys are dead "
                "(replug a keyboard and restart voicisst)",
                file=sys.stderr,
            )

    def _prune_dead_fds(self) -> None:
        for fd in list(self._fd_to_dev):
            try:
                select.select([fd], [], [], 0)
            except (OSError, ValueError) as e:
                self._drop_device(fd, e)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _close_quietly(dev: Any) -> None:
        try:
            dev.close()
        except Exception:
            pass

    @staticmethod
    def _safe(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as e:
            print(f"voicisst hotkeys: callback error: {e}", file=sys.stderr)
