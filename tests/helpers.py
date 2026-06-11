"""Shared test helpers. Import-light: numpy + flow_dictation only.

Other test files do `from helpers import FakeEngine, make_wav, ...`.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any

import numpy as np

from flow_dictation.engine.base import Engine, StreamSession
from flow_dictation.protocol import encode_wav


def make_audio(seconds: float = 0.5, sr: int = 16000) -> np.ndarray:
    """Deterministic float32 mono sine, `seconds` long at `sr`."""
    t = np.arange(int(seconds * sr), dtype=np.float32) / float(sr)
    return (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


def make_wav(seconds: float = 0.5, sr: int = 16000) -> bytes:
    """16-bit PCM mono WAV bytes for the same sine."""
    return encode_wav(make_audio(seconds, sr), sr)


class FakeStreamSession(StreamSession):
    """Scripted session: partials pop from a list; finalize polishes raw."""

    def __init__(
        self, owner: FakeEngine, sample_rate: int, language: str | None, vocab: str
    ):
        self.owner = owner
        self.sample_rate = sample_rate
        self.language = language
        self.vocab = vocab
        self.fed: list[np.ndarray] = []
        self.partials: list[str] = list(owner.scripted_partials)
        self.cancelled = False
        self.closed = False

    def feed(self, chunk: np.ndarray) -> None:
        self.fed.append(np.asarray(chunk, dtype=np.float32).reshape(-1))

    def partial(self) -> str | None:
        return self.partials.pop(0) if self.partials else None

    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        n = int(sum(c.size for c in self.fed))
        raw = self.owner.transcribe(
            np.zeros(n, dtype=np.float32),
            self.sample_rate,
            language=self.language,
            vocab=vocab or self.vocab,
        )
        yield from self.owner.polish_stream(
            raw, language=self.language, vocab=vocab or self.vocab
        )

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


class FakeEngine(Engine):
    """Deterministic engine: every output encodes its inputs; records calls."""

    def __init__(self, *, supports_stream: bool = True):
        self.calls: list[tuple] = []
        self.sessions: list[FakeStreamSession] = []
        self.scripted_partials: list[str] = []
        self.supports_stream = supports_stream
        self.warmed = 0
        self.closed = 0

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        language: str | None = None,
        vocab: str = "",
    ) -> str:
        n = int(np.asarray(audio).size)
        self.calls.append(("transcribe", n, sample_rate, language, vocab))
        return f"raw:{n}@{sample_rate}:{language}:{vocab}"

    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        self.calls.append(("polish", text, language, vocab))
        return f"polished:{text}"

    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        self.calls.append(("polish_stream", text, language, vocab))
        yield f"polishing:{text}"
        yield f"polished:{text}"

    def open_stream(
        self, sample_rate: int, *, language: str | None = None, vocab: str = ""
    ) -> StreamSession | None:
        if not self.supports_stream:
            return None
        session = FakeStreamSession(self, sample_rate, language, vocab)
        self.sessions.append(session)
        return session

    def health(self) -> dict:
        self.calls.append(("health",))
        return {
            "status": "ok",
            "version": "test",
            "mode": "fake",
            "whisper_model": "fake-model",
            "device": "cpu",
            "polish_backend": "fake",
            "polish_model": "fake-polish",
        }

    def warm(self) -> None:
        self.warmed += 1

    def close(self) -> None:
        self.closed += 1


# ---------------------------------------------------------------------------
# Fake heavyweight modules for LocalEngine tests. Inject with:
#   monkeypatch.setitem(sys.modules, "flow_dictation.transcribe", fake_transcribe_module(rec))
#   monkeypatch.setitem(sys.modules, "flow_dictation.polish", fake_polish_module(rec))


def fake_transcribe_module(record: dict[str, Any]) -> types.ModuleType:
    record.setdefault("transcribers", [])
    mod = types.ModuleType("flow_dictation.transcribe")

    class Transcriber:
        def __init__(self, wcfg: Any):
            self.cfg = wcfg
            self.model_name = "fake-model"
            self.device = "cpu"
            self.calls: list[dict[str, Any]] = []
            record["transcribers"].append(self)

        def transcribe(
            self,
            audio: np.ndarray,
            sample_rate: int = 16000,
            *,
            language: str | None = None,
            vocab: str = "",
        ) -> str:
            n = int(np.asarray(audio).size)
            self.calls.append(
                {"n": n, "sample_rate": sample_rate, "language": language, "vocab": vocab}
            )
            return f"t:{n}:{language}:{vocab}"

    mod.Transcriber = Transcriber  # type: ignore[attr-defined]
    return mod


def fake_polish_module(record: dict[str, Any]) -> types.ModuleType:
    record.setdefault("polishers", [])
    record.setdefault("watchdogs", [])
    mod = types.ModuleType("flow_dictation.polish")

    class FakePolisher:
        def __init__(self, pcfg: Any = None) -> None:
            self.warm_calls = 0
            self.unload_calls = 0
            self.calls: list[tuple] = []
            record["polishers"].append(self)
            # Mirror OllamaPolisher: the polisher owns the watchdog.
            self.watchdog = VramWatchdog(pcfg)
            self.watchdog.start()

        def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
            self.calls.append(("polish", text, language, vocab))
            return f"p:{text}"

        def polish_stream(
            self, text: str, *, language: str | None = None, vocab: str = ""
        ) -> Iterator[str]:
            self.calls.append(("polish_stream", text, language, vocab))
            yield f"s1:{text}"
            yield f"p:{text}"

        def warm(self) -> None:
            self.warm_calls += 1

        def unload(self) -> None:
            self.unload_calls += 1

    class VramWatchdog:
        def __init__(self, pcfg: Any):
            self.cfg = pcfg
            self.started = False
            record["watchdogs"].append(self)

        def start(self) -> None:
            # Mirror the real watchdog: a no-op unless the threshold is set.
            if self.cfg is not None and getattr(self.cfg, "vram_unload_below_mb", 0) > 0:
                self.started = True

        def stop(self) -> None:
            self.started = False

    def get_polisher(cfg: Any) -> FakePolisher | None:
        if not cfg.polish.enabled or cfg.polish.backend == "none":
            return None
        return FakePolisher(cfg.polish)

    mod.FakePolisher = FakePolisher  # type: ignore[attr-defined]
    mod.VramWatchdog = VramWatchdog  # type: ignore[attr-defined]
    mod.get_polisher = get_polisher  # type: ignore[attr-defined]
    return mod
