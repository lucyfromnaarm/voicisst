"""Engine contract: the boundary between capture/injection and inference.

LocalEngine runs Whisper + the polish LLM in-process (all-in-one mode).
RemoteEngine speaks the Flow HTTP/WS protocol to a `flow serve` instance
(client mode). The dictation app only ever sees this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


class EngineError(RuntimeError):
    """Engine failure with a user-facing fix suggestion in `.hint`."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class StreamSession(ABC):
    """A live utterance: audio chunks in, partial transcripts out.

    Used by streaming mode (live typing while speaking). Sessions are
    single-use: feed() during recording, then finalize() or cancel().
    """

    @abstractmethod
    def feed(self, chunk: np.ndarray) -> None:
        """Append a float32 mono chunk of the in-progress utterance."""

    @abstractmethod
    def partial(self) -> str | None:
        """Latest full raw transcript, or None if unchanged/not ready.

        Must never block longer than roughly one transcription pass and
        must be safe to call repeatedly from a ticker thread.
        """

    @abstractmethod
    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        """Finish the utterance. Yields successive FULL-TEXT polished
        snapshots; the last yield is the final text. If polish is
        disabled or fails, yields the raw transcript once.
        """

    @abstractmethod
    def cancel(self) -> None:
        """Abort the session; no further output."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Idempotent."""


class Engine(ABC):
    """Transcription + polish, local or remote."""

    @abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        language: str | None = None,
        vocab: str = "",
    ) -> str:
        """Transcribe float32 mono audio. language=None -> auto-detect.
        `vocab` is spelling context (names, jargon)."""

    @abstractmethod
    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        """Rework raw speech into clear written text. On failure returns
        `text` unchanged (never raises for polish-level errors)."""

    @abstractmethod
    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        """Like polish() but yields successive full-text snapshots; the
        last yield is final. On failure yields `text` once."""

    def open_stream(
        self, sample_rate: int, *, language: str | None = None, vocab: str = ""
    ) -> StreamSession | None:
        """Start a live-transcription session, or None if unsupported."""
        return None

    @abstractmethod
    def health(self) -> dict:
        """Status info: {"status", "version", "mode", "whisper_model", ...}.
        Raises EngineError when the engine is unreachable/broken."""

    def warm(self) -> None:  # noqa: B027 — optional hook, default no-op
        """Preload models so the first utterance is fast. Best-effort."""

    def close(self) -> None:  # noqa: B027 — optional hook, default no-op
        """Release resources. Idempotent."""
