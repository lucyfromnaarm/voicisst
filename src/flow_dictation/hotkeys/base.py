"""Hotkey listener contract: global press/release detection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence


class HotkeyListener(ABC):
    """Watches for the dictation hotkey(s) globally.

    Reports raw press/release; hold-vs-toggle semantics live in the
    dictation app. `on_backspace` (optional) fires on any Backspace press
    anywhere — used to cancel an in-flight polish.

    Implementations run their own thread; start() must not block.
    Autorepeat must be suppressed (one on_press per physical press).
    """

    def __init__(
        self,
        keys: Sequence[str],
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_backspace: Callable[[], None] | None = None,
    ):
        self.keys = list(keys)
        self.on_press = on_press
        self.on_release = on_release
        self.on_backspace = on_backspace

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Can this backend work right now (permissions, display server)?"""

    @abstractmethod
    def start(self) -> None:
        """Begin listening (non-blocking)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and join the thread. Idempotent."""
