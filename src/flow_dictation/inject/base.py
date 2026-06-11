"""Injector contract: putting text into the focused window."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import OutputConfig


class Injector(ABC):
    """A way to send keystrokes/text to whatever has focus.

    Implementations must be safe to construct cheaply; expensive checks
    belong in available(). All methods return True on success and must
    never raise for environmental failures (missing daemon, etc.).
    """

    name: str = "base"

    def __init__(self, output_cfg: OutputConfig):
        self.cfg = output_cfg

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Can this backend work in the current environment right now?"""

    @abstractmethod
    def type_text(self, text: str) -> bool:
        """Type literal text. Newlines are translated according to
        cfg.newline_mode ("shift-enter" sends Shift+Enter so chat apps
        don't submit)."""

    @abstractmethod
    def backspace(self, n: int) -> bool:
        """Send exactly n backspaces. Never more than n."""

    @abstractmethod
    def paste_chord(self) -> bool:
        """Press the platform paste shortcut (Ctrl+V / Cmd+V). The text
        must already be on the clipboard."""

    def tap_escape(self) -> bool:
        """Best-effort Escape tap (used to clear a selection)."""
        return False
