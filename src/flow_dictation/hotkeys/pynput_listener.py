"""pynput-based global hotkey listener (macOS, Windows, Linux/X11).

Runs a `pynput.keyboard.Listener` in non-suppress mode (keys still reach
the focused app). Key names accepted in config:

- pynput special names: "alt_r", "ctrl_r", "f9", "menu", ...
- single characters: "a", "§", ...
- evdev-style names ("KEY_MENU", "KEY_RIGHTALT", ...) — mapped to the
  pynput equivalent where one exists, so a Linux config keeps working on
  macOS/Windows.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from ..engine.base import EngineError
from .base import HotkeyListener

# evdev "KEY_XXX" -> pynput Key attribute name (where an equivalent exists).
_EVDEV_TO_PYNPUT: dict[str, str] = {
    "KEY_MENU": "menu",
    "KEY_COMPOSE": "menu",
    "KEY_RIGHTALT": "alt_r",
    "KEY_LEFTALT": "alt_l",
    "KEY_RIGHTCTRL": "ctrl_r",
    "KEY_LEFTCTRL": "ctrl_l",
    "KEY_RIGHTSHIFT": "shift_r",
    "KEY_LEFTSHIFT": "shift_l",
    "KEY_RIGHTMETA": "cmd_r",
    "KEY_LEFTMETA": "cmd",
    "KEY_CAPSLOCK": "caps_lock",
    "KEY_SCROLLLOCK": "scroll_lock",
    "KEY_NUMLOCK": "num_lock",
    "KEY_PAUSE": "pause",
    "KEY_INSERT": "insert",
    "KEY_DELETE": "delete",
    "KEY_HOME": "home",
    "KEY_END": "end",
    "KEY_PAGEUP": "page_up",
    "KEY_PAGEDOWN": "page_down",
    "KEY_ESC": "esc",
    "KEY_TAB": "tab",
    "KEY_SPACE": "space",
    "KEY_ENTER": "enter",
    "KEY_BACKSPACE": "backspace",
    "KEY_UP": "up",
    "KEY_DOWN": "down",
    "KEY_LEFT": "left",
    "KEY_RIGHT": "right",
}
_EVDEV_TO_PYNPUT.update({f"KEY_F{i}": f"f{i}" for i in range(1, 25)})


def _evdev_to_pynput_name(name: str) -> str | None:
    """Map an upper-cased 'KEY_XXX' evdev name to a pynput name, or None."""
    mapped = _EVDEV_TO_PYNPUT.get(name)
    if mapped is not None:
        return mapped
    tail = name[len("KEY_") :]
    if len(tail) == 1:  # KEY_A, KEY_1, ...
        return tail.lower()
    return None


def resolve_key_name(name: str) -> Any | None:
    """Resolve a configured key name to a pynput key object, or None.

    Accepts pynput special names ('alt_r', 'f9', 'menu'), single
    characters, and evdev-style 'KEY_XXX' names.
    """
    from pynput.keyboard import Key, KeyCode

    raw = name.strip()
    if not raw:
        return None
    if raw.upper().startswith("KEY_") and len(raw) > len("KEY_"):
        mapped = _evdev_to_pynput_name(raw.upper())
        if mapped is None:
            return None
        raw = mapped
    if len(raw) == 1:
        return KeyCode.from_char(raw.lower())
    return getattr(Key, raw.lower(), None)


class PynputListener(HotkeyListener):
    """Global hotkey listener via pynput, with OS-autorepeat suppression.

    pynput delivers OS autorepeat as repeated on_press callbacks without a
    release; a held-key set keeps it to one on_press per physical press.
    Holder arbitration mirrors the evdev backend: the first held hotkey
    wins until it is released. Backspace presses are ALWAYS forwarded to
    `on_backspace`; the app decides whether they matter.
    """

    name = "pynput"

    def __init__(
        self,
        keys: Sequence[str],
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_backspace: Callable[[], None] | None = None,
    ):
        super().__init__(keys, on_press, on_release, on_backspace)
        self._listener: Any = None
        self._hotkeys: list[Any] = []
        # Equality-based containers (pynput KeyCode equality is richer than
        # identity; lists keep the semantics obvious and hash-free).
        self._held: list[Any] = []
        self._holder: Any = None
        self._backspace_key: Any = None

    # -- availability -----------------------------------------------------

    @classmethod
    def available(cls) -> bool:
        if sys.platform == "linux" and not os.environ.get("DISPLAY"):
            # pynput needs X11. On pure Wayland without an XWayland DISPLAY
            # it cannot see global key events — use the evdev backend there.
            return False
        try:
            import pynput  # noqa: F401
        except Exception:
            # ImportError, or pynput's backend probing failing headless.
            return False
        return True

    # -- key resolution -----------------------------------------------------

    def _resolve_keys(self) -> list[Any]:
        resolved: list[Any] = []
        for name in self.keys:
            key = resolve_key_name(name)
            if key is None:
                print(
                    f"flow hotkeys: cannot map hotkey name {name!r} to a pynput key — "
                    "skipping (try 'alt_r', 'ctrl_r', 'menu', 'f9', or a single character)",
                    file=sys.stderr,
                )
            elif not self._contains(resolved, key):
                resolved.append(key)
        if not resolved:
            raise EngineError(
                f"none of the configured hotkeys {self.keys} map to pynput keys",
                hint="set hotkey.keys to pynput names like \"alt_r\", \"ctrl_r\", \"f9\" "
                "or single characters",
            )
        if sys.platform == "win32":
            self._add_win32_altgr_companion(resolved)
        return resolved

    @classmethod
    def _add_win32_altgr_companion(cls, resolved: list[Any]) -> None:
        """Watch alt_gr alongside alt_r on Windows (and vice versa).

        pynput's win32 listener maps vk 0xA5 (right Alt) to Key.alt_gr —
        Key.alt_gr is defined after Key.alt_r with the same vk, so it wins
        in Listener._SPECIAL_KEYS. A configured 'alt_r' would therefore
        never fire; watching both keys makes either name work.
        """
        from pynput.keyboard import Key

        alt_r = getattr(Key, "alt_r", None)
        alt_gr = getattr(Key, "alt_gr", None)
        if alt_r is None or alt_gr is None:
            return
        has_alt_r = cls._contains(resolved, alt_r)
        has_alt_gr = cls._contains(resolved, alt_gr)
        if has_alt_r and not has_alt_gr:
            resolved.append(alt_gr)
        elif has_alt_gr and not has_alt_r:
            resolved.append(alt_r)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._listener is not None:
            return
        from pynput import keyboard

        self._hotkeys = self._resolve_keys()
        self._backspace_key = keyboard.Key.backspace
        self._held = []
        self._holder = None
        listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
            suppress=False,  # never swallow keys: dictation must not break typing
        )
        listener.start()
        self._listener = listener
        if sys.platform == "darwin":
            self._warn_if_untrusted_darwin(listener)

    def _warn_if_untrusted_darwin(self, listener: Any) -> None:
        """Surface a macOS Input-Monitoring denial, which pynput swallows.

        Without the permission, pynput's darwin listener thread fails to
        create its event tap and exits silently — hotkeys would just never
        fire. The listener thread sets IS_TRUSTED (HIServices.
        AXIsProcessTrusted via pynput._util.darwin) early in its run loop,
        before marking itself ready; wait briefly for that, then warn.
        """
        deadline = time.monotonic() + 2.0
        while not getattr(listener, "_ready", True):
            alive = getattr(listener, "is_alive", None)
            if (callable(alive) and not alive()) or time.monotonic() > deadline:
                break
            time.sleep(0.01)
        if getattr(listener, "IS_TRUSTED", True):
            return
        summary = "flow hotkeys: macOS denied Input Monitoring"
        body = (
            "grant your terminal/app permission in System Settings -> "
            "Privacy & Security -> Input Monitoring, then restart flow"
        )
        print(f"{summary} — {body}", file=sys.stderr)
        try:
            from ..notify import notify

            notify(summary, body, urgency="critical")
        except Exception as e:
            print(f"flow hotkeys: notification failed: {e}", file=sys.stderr)

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
                listener.join(2.0)
            except Exception:
                pass  # pynput re-raises stored callback exceptions in join()
        self._held = []
        self._holder = None

    # -- event handling ---------------------------------------------------------

    def _canon(self, key: Any) -> Any:
        """Normalize character keys so 'X' (shifted) matches hotkey 'x'."""
        char = getattr(key, "char", None)
        if char:
            try:
                from pynput.keyboard import KeyCode

                return KeyCode.from_char(char.lower())
            except Exception:
                return key
        return key

    # NOTE on the `injected` parameter: pynput >= 1.8 wraps callbacks with
    # Listener._wrap(f, 2) and — when the callback accepts two arguments —
    # passes (key, injected) positionally, where `injected` is True for
    # events synthesized by software (e.g. Flow's own backspaces during
    # streaming polish). Those must be ignored, or our injected backspaces
    # would self-cancel the polish. The default value keeps the signature
    # compatible with older pynput, which passes only `key`.

    def _handle_press(self, key: Any, injected: bool = False) -> None:
        if injected:  # our own synthetic keystrokes must not trigger hotkeys
            return
        if key is None:  # pynput reports unknown keys as None
            return
        k = self._canon(key)
        if self._backspace_key is not None and k == self._backspace_key:
            if self._contains(self._held, k):
                return  # OS autorepeat
            self._held.append(k)
            self._safe(self.on_backspace)
            return
        if not self._contains(self._hotkeys, k):
            return
        if self._contains(self._held, k):
            return  # OS autorepeat
        self._held.append(k)
        if self._holder is None:
            self._holder = k
            self._safe(self.on_press)

    def _handle_release(self, key: Any, injected: bool = False) -> None:
        if injected:
            return
        if key is None:
            return
        k = self._canon(key)
        self._discard(self._held, k)
        if self._contains(self._hotkeys, k) and self._holder == k:
            self._holder = None
            self._safe(self.on_release)

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _contains(seq: Sequence[Any], key: Any) -> bool:
        return any(k == key for k in seq)

    @staticmethod
    def _discard(seq: list[Any], key: Any) -> None:
        for i, k in enumerate(seq):
            if k == key:
                del seq[i]
                return

    @staticmethod
    def _safe(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as e:
            print(f"flow hotkeys: callback error: {e}", file=sys.stderr)
