"""Audio-file transcription pipeline: decoder-independent behavior."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from helpers import FakeEngine, make_wav
from voicisst.config import Config
from voicisst.files import (
    FILE_SAMPLE_RATE,
    collect_file_vocab,
    iter_audio_file_chunks,
    process_audio_file,
)


def write_wav(path: Path, seconds: float = 0.5, sr: int = 16000) -> None:
    path.write_bytes(make_wav(seconds, sr))


def test_collect_file_vocab_uses_config_and_dictionary_file(
    cfg: Config, tmp_path: Path
) -> None:
    dictionary = tmp_path / "dictionary.txt"
    dictionary.write_text("Naarm\n# comment\nKubernetes\n", encoding="utf-8")
    cfg.dictionary.path = str(dictionary)
    cfg.dictionary.words = ["Octavia"]
    assert collect_file_vocab(cfg) == "Octavia, Naarm, Kubernetes"


def test_wav_file_chunks_resample_and_split(tmp_path: Path) -> None:
    path = tmp_path / "sample.wav"
    write_wav(path, seconds=2.0, sr=8000)
    chunks = list(iter_audio_file_chunks(path, chunk_seconds=1.0))
    assert len(chunks) == 2
    assert all(c.dtype == np.float32 for c in chunks)
    assert sum(c.size for c in chunks) == FILE_SAMPLE_RATE * 2


def test_process_audio_file_transcribes_chunks_then_polishes(
    cfg: Config, tmp_path: Path
) -> None:
    path = tmp_path / "sample.wav"
    write_wav(path, seconds=2.0)
    cfg.audio.normalize = False
    cfg.dictionary.words = ["Lucy"]
    engine = FakeEngine()
    result = process_audio_file(path, cfg, engine, chunk_seconds=1.0)
    raw1 = f"raw:{FILE_SAMPLE_RATE}@{FILE_SAMPLE_RATE}:None:Lucy"
    raw = raw1 + "\n\n" + raw1
    assert result.raw == raw
    assert result.text == f"polished:{raw}"
    assert result.chunks == 2
    assert ("polish", raw, None, "Lucy") in engine.calls


def test_process_audio_file_can_skip_polish(cfg: Config, tmp_path: Path) -> None:
    path = tmp_path / "sample.wav"
    write_wav(path, seconds=0.5)
    cfg.audio.normalize = False
    engine = FakeEngine()
    result = process_audio_file(path, cfg, engine, polish=False)
    assert result.text == result.raw
    assert not any(c[0] == "polish" for c in engine.calls)


def test_wav_fallback_rejects_non_16_bit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "av", None)
    monkeypatch.setattr("voicisst.files.shutil.which", lambda name: None)
    path = tmp_path / "eight-bit.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(16000)
        w.writeframes(bytes(100))
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="16-bit"):
        list(iter_audio_file_chunks(path))
