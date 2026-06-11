"""LocalEngine: in-process Whisper + polish LLM (all-in-one mode).

Models are heavy, so everything is built lazily on first use with
double-checked locking; `warm()` preloads the whole stack up front.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import __version__
from .base import Engine, EngineError, StreamSession

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..config import Config

# A streaming partial pass needs at least this much audio to be worth a
# whisper call (ported from the prototype's StreamingTyper loop).
_MIN_PARTIAL_S = 0.5


class LocalEngine(Engine):
    """Wraps `transcribe.Transcriber` and `polish.get_polisher(cfg)`."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._init_lock = threading.Lock()
        self._transcriber: Any = None
        self._polisher: Any = None
        self._polisher_ready = False  # get_polisher() may legitimately return None
        self._watchdog: Any = None

    # -- lazy construction ------------------------------------------------

    def _ensure_transcriber(self) -> Any:
        t = self._transcriber
        if t is not None:
            return t
        with self._init_lock:
            if self._transcriber is None:
                try:
                    from voicisst.transcribe import Transcriber
                except ImportError as e:
                    raise EngineError(
                        f"local transcription unavailable: {e}",
                        hint="install the local extra: pip install 'voicisst[local]' "
                        "— or use a server: voicisst run --server http://<host>:8765",
                    ) from e
                try:
                    self._transcriber = Transcriber(self._cfg.whisper)
                except EngineError:
                    raise
                except Exception as e:
                    raise EngineError(
                        f"failed to load Whisper model {self._cfg.whisper.model!r}: {e}",
                        hint="check [whisper] model/device in config.toml; "
                        "try device = 'cpu' or a smaller model like 'small'",
                    ) from e
            return self._transcriber

    def _ensure_polisher(self) -> Any:
        if self._polisher_ready:
            return self._polisher
        with self._init_lock:
            if not self._polisher_ready:
                try:
                    from voicisst.polish import get_polisher

                    self._polisher = get_polisher(self._cfg)
                except Exception as e:
                    print(
                        f"voicisst: polish unavailable ({e}); using raw transcripts",
                        file=sys.stderr,
                    )
                    self._polisher = None
                self._polisher_ready = True
            return self._polisher

    def _language(self, language: str | None) -> str | None:
        # Explicit argument wins; otherwise fall back to the configured one.
        return language if language is not None else self._cfg.whisper.language_or_none()

    # -- Engine API ---------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        language: str | None = None,
        vocab: str = "",
    ) -> str:
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return ""
        t = self._ensure_transcriber()
        try:
            return str(
                t.transcribe(arr, sample_rate, language=self._language(language), vocab=vocab)
            ).strip()
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(
                f"transcription failed: {e}",
                hint="if this is a GPU/VRAM error, set [whisper] device = 'cpu' "
                "or pick a smaller model",
            ) from e

    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        if not text:
            return text
        p = self._ensure_polisher()
        if p is None:
            return text
        try:
            return str(p.polish(text, language=self._language(language), vocab=vocab))
        except Exception as e:
            print(f"voicisst: polish failed ({e}); using raw transcript", file=sys.stderr)
            return text

    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        p = self._ensure_polisher()
        if p is None or not text:
            yield text
            return
        try:
            yield from p.polish_stream(text, language=self._language(language), vocab=vocab)
        except Exception as e:
            print(f"voicisst: polish stream failed ({e}); using raw transcript", file=sys.stderr)
            yield text

    def open_stream(
        self, sample_rate: int, *, language: str | None = None, vocab: str = ""
    ) -> StreamSession | None:
        return LocalStreamSession(self, sample_rate, self._language(language), vocab)

    def health(self) -> dict:
        t = self._transcriber
        polish_backend = self._cfg.polish.backend if self._cfg.polish.enabled else "none"
        if self._polisher_ready and self._polisher is None:
            polish_backend = "none"
        return {
            "status": "ok",
            "version": __version__,
            "mode": "local",
            "whisper_model": t.model_name if t is not None else self._cfg.whisper.model,
            "device": t.device if t is not None else self._cfg.whisper.device,
            "polish_backend": polish_backend,
            "polish_model": self._cfg.polish.model if polish_backend != "none" else "",
        }

    def warm(self) -> None:
        """Preload Whisper + the polisher (which owns the VRAM watchdog)."""
        self._ensure_transcriber()
        p = self._ensure_polisher()
        if p is not None:
            try:
                p.warm()
            except Exception as e:
                print(f"voicisst: polish warm-up failed ({e})", file=sys.stderr)
            # OllamaPolisher starts its own VramWatchdog; keep a reference
            # so close() can stop it. Never start a second one here.
            self._watchdog = getattr(p, "watchdog", None)

    def close(self) -> None:
        if self._watchdog is not None:
            try:
                self._watchdog.stop()
            except Exception:
                pass
            self._watchdog = None
        if self._polisher is not None:
            try:
                self._polisher.unload()
            except Exception:
                pass
            self._polisher = None
            self._polisher_ready = False
        self._transcriber = None


class LocalStreamSession(StreamSession):
    """Accumulates fed chunks; `partial()` re-transcribes the whole buffer.

    `partial()` uses a non-blocking lock: if a transcription pass is already
    running it returns None immediately (the caller's ticker just skips, like
    the prototype's StreamingTyper). `finalize()` takes the same lock
    *blocking* so the final pass never overlaps an in-flight partial — the
    Whisper model must not be called from two threads at once.
    """

    def __init__(
        self, engine: LocalEngine, sample_rate: int, language: str | None, vocab: str
    ):
        self._engine = engine
        self._sr = int(sample_rate)
        self._language = language
        self._vocab = vocab
        self._chunks: list[np.ndarray] = []
        self._buf_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._last_partial = ""
        self._closed = False

    def _snapshot(self) -> np.ndarray:
        with self._buf_lock:
            chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)

    def feed(self, chunk: np.ndarray) -> None:
        if self._closed:
            return
        arr = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        with self._buf_lock:
            self._chunks.append(arr)

    def partial(self) -> str | None:
        if self._closed:
            return None
        if not self._run_lock.acquire(blocking=False):
            return None  # previous pass still running — skip this tick
        try:
            audio = self._snapshot()
            if audio.size < int(self._sr * _MIN_PARTIAL_S):
                return None
            try:
                text = self._engine.transcribe(
                    audio, self._sr, language=self._language, vocab=self._vocab
                )
            except Exception as e:
                print(f"voicisst: stream transcribe error: {e}", file=sys.stderr)
                return None
            if not text or text == self._last_partial:
                return None
            self._last_partial = text
            return text
        finally:
            self._run_lock.release()

    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        if self._closed:
            return
        final_vocab = vocab or self._vocab
        # Blocking acquire: wait out any in-flight partial pass first.
        with self._run_lock:
            self._closed = True
            audio = self._snapshot()
            with self._buf_lock:
                self._chunks = []
            raw = ""
            if audio.size:
                raw = self._engine.transcribe(
                    audio, self._sr, language=self._language, vocab=final_vocab
                )
        if not raw:
            yield raw
            return
        yield from self._engine.polish_stream(raw, language=self._language, vocab=final_vocab)

    def cancel(self) -> None:
        self._closed = True
        with self._buf_lock:
            self._chunks = []

    def close(self) -> None:
        self._closed = True
        with self._buf_lock:
            self._chunks = []
