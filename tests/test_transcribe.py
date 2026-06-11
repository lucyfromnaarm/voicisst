"""Tests for voicisst.transcribe — fake faster_whisper/ctranslate2,
no GPU, no model downloads, no audio hardware."""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from voicisst.config import WhisperConfig
from voicisst.transcribe import Transcriber, _pick_device

# ---------------------------------------------------------------------------
# Fakes


class FakeSegment:
    def __init__(self, text: str):
        self.text = text


class FakeWhisperModel:
    instances: list[FakeWhisperModel] = []

    def __init__(self, model_name: str, device: str = "", compute_type: str = ""):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.calls: list[tuple[np.ndarray, dict[str, Any]]] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio: np.ndarray, **kwargs: Any):
        self.calls.append((audio, kwargs))
        return iter([FakeSegment(" hello"), FakeSegment(" world ")]), {"language": "en"}


class ExplodingWhisperModel:
    def __init__(self, *a: Any, **k: Any):
        raise RuntimeError("kaboom")


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    FakeWhisperModel.instances = []
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return mod


def fake_ctranslate2(monkeypatch: pytest.MonkeyPatch, cuda_devices: int) -> None:
    mod = types.ModuleType("ctranslate2")
    mod.get_cuda_device_count = lambda: cuda_devices
    monkeypatch.setitem(sys.modules, "ctranslate2", mod)


def broken_ctranslate2(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise RuntimeError("driver mismatch")

    mod = types.ModuleType("ctranslate2")
    mod.get_cuda_device_count = boom
    monkeypatch.setitem(sys.modules, "ctranslate2", mod)


# ---------------------------------------------------------------------------
# Device / model resolution


def test_auto_cpu_resolves_small(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 0)
    t = Transcriber(WhisperConfig())  # model="auto", device="auto"
    assert t.device == "cpu"
    assert t.compute == "int8"
    assert t.model_name == "small"
    model = FakeWhisperModel.instances[0]
    assert model.model_name == "small"
    assert model.device == "cpu"
    assert model.compute_type == "int8"


def test_auto_cuda_resolves_large_v3_turbo(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 1)
    t = Transcriber(WhisperConfig())
    assert t.device == "cuda"
    assert t.compute == "float16"
    assert t.model_name == "large-v3-turbo"


def test_explicit_model_wins_over_auto(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 1)
    t = Transcriber(WhisperConfig(model="medium.en"))
    assert t.model_name == "medium.en"


def test_explicit_device_skips_cuda_probe(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    broken_ctranslate2(monkeypatch)  # would raise if probed
    t = Transcriber(WhisperConfig(device="cpu"))
    assert t.device == "cpu"


def test_broken_cuda_probe_falls_back_to_cpu(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    broken_ctranslate2(monkeypatch)
    t = Transcriber(WhisperConfig(device="auto"))
    assert t.device == "cpu"
    assert t.model_name == "small"


def test_compute_override_passes_through(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 0)
    t = Transcriber(WhisperConfig(compute="int8_float16"))
    assert t.compute == "int8_float16"
    assert FakeWhisperModel.instances[0].compute_type == "int8_float16"


def test_pick_device_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ctranslate2(monkeypatch, 2)
    assert _pick_device(WhisperConfig()) == ("cuda", "float16")
    assert _pick_device(WhisperConfig(device="cpu")) == ("cpu", "int8")


# ---------------------------------------------------------------------------
# Helpful errors


def test_missing_faster_whisper_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ctranslate2(monkeypatch, 0)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import -> ImportError
    with pytest.raises(RuntimeError, match=r"voicisst\[local\]"):
        Transcriber(WhisperConfig())


def test_model_load_failure_has_hint(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 0)
    fake_whisper.WhisperModel = ExplodingWhisperModel
    with pytest.raises(RuntimeError) as exc:
        Transcriber(WhisperConfig())
    assert "kaboom" in str(exc.value)
    assert "model name" in str(exc.value)  # cpu hint: check the model name


def test_cuda_load_failure_suggests_cpu(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 1)
    fake_whisper.WhisperModel = ExplodingWhisperModel
    with pytest.raises(RuntimeError, match='device = "cpu"'):
        Transcriber(WhisperConfig())


# ---------------------------------------------------------------------------
# transcribe()


def make_transcriber(monkeypatch: pytest.MonkeyPatch) -> Transcriber:
    fake_ctranslate2(monkeypatch, 0)
    return Transcriber(WhisperConfig())


def test_transcribe_joins_and_strips_segments(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    out = t.transcribe(np.zeros(16000, dtype=np.float32))
    assert out == "hello world"


def test_transcribe_forwards_language_vocab_beam(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    fake_ctranslate2(monkeypatch, 0)
    t = Transcriber(WhisperConfig(beam_size=2, vad_filter=True))
    t.transcribe(np.zeros(16000, dtype=np.float32), language="es", vocab="Lucy, Naarm")
    _audio, kwargs = FakeWhisperModel.instances[0].calls[0]
    assert kwargs["language"] == "es"
    assert kwargs["initial_prompt"] == "Lucy, Naarm"
    assert kwargs["beam_size"] == 2
    assert kwargs["vad_filter"] is True


def test_transcribe_defaults_autodetect_no_prompt(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    t.transcribe(np.zeros(16000, dtype=np.float32))
    _audio, kwargs = FakeWhisperModel.instances[0].calls[0]
    assert kwargs["language"] is None  # None = Whisper auto-detect
    assert kwargs["initial_prompt"] is None
    assert kwargs["beam_size"] == 5
    assert kwargs["vad_filter"] is False


def test_transcribe_truncates_vocab(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    t.transcribe(np.zeros(16000, dtype=np.float32), vocab="x" * 5000)
    _audio, kwargs = FakeWhisperModel.instances[0].calls[0]
    assert len(kwargs["initial_prompt"]) == 800


def test_transcribe_resamples_48k(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    resampled = np.zeros(16000, dtype=np.float32)
    calls: list[tuple[int, int]] = []

    def fake_resample(audio: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
        calls.append((sr_from, sr_to))
        return resampled

    fake_audio = types.ModuleType("voicisst.audio")
    fake_audio.resample = fake_resample
    monkeypatch.setitem(sys.modules, "voicisst.audio", fake_audio)

    t.transcribe(np.zeros(48000, dtype=np.float32), sample_rate=48000)
    assert calls == [(48000, 16000)]
    model_audio, _kwargs = FakeWhisperModel.instances[0].calls[0]
    assert model_audio is resampled  # the resampled buffer reaches the model


def test_transcribe_no_resample_at_16k(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)

    fake_audio = types.ModuleType("voicisst.audio")

    def fail_resample(*a: object) -> np.ndarray:
        raise AssertionError("resample must not be called at 16 kHz")

    fake_audio.resample = fail_resample
    monkeypatch.setitem(sys.modules, "voicisst.audio", fake_audio)

    assert t.transcribe(np.zeros(16000, dtype=np.float32), sample_rate=16000) == "hello world"


def test_transcribe_empty_audio_short_circuits(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    assert t.transcribe(np.zeros(0, dtype=np.float32)) == ""
    assert FakeWhisperModel.instances[0].calls == []


def test_transcribe_coerces_dtype_and_shape(
    monkeypatch: pytest.MonkeyPatch, fake_whisper: types.ModuleType
) -> None:
    t = make_transcriber(monkeypatch)
    t.transcribe(np.zeros((16000, 1), dtype=np.float64))
    model_audio, _kwargs = FakeWhisperModel.instances[0].calls[0]
    assert model_audio.dtype == np.float32
    assert model_audio.ndim == 1
