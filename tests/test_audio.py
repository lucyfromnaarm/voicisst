"""Headless tests for flow_dictation.audio (no real audio hardware)."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from flow_dictation import audio

# ---------------------------------------------------------------------------
# Fake sounddevice


class FakeInputStream:
    def __init__(self, **kwargs: object):
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        self.started = False
        self.stopped = False
        self.closed = False
        self.fail_start = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("PortAudio: device unavailable")
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    def close(self) -> None:
        self.closed = True


def make_fake_sd(*, fail_init: bool = False, fail_start: bool = False, fail_play: bool = False):
    mod = types.ModuleType("sounddevice")
    mod.streams = []
    mod.played = []

    def input_stream(**kwargs: object) -> FakeInputStream:
        if fail_init:
            raise RuntimeError("Error querying device")
        s = FakeInputStream(**kwargs)
        s.fail_start = fail_start
        mod.streams.append(s)
        return s

    def play(data, samplerate=None, **kwargs: object) -> None:
        if fail_play:
            raise RuntimeError("no output device")
        mod.played.append((np.asarray(data), samplerate))

    mod.InputStream = input_stream
    mod.play = play
    return mod


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch):
    mod = make_fake_sd()
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    return mod


# ---------------------------------------------------------------------------
# rms


def test_rms_empty_is_zero() -> None:
    assert audio.rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_rms_silence_is_zero() -> None:
    assert audio.rms(np.zeros(1000, dtype=np.float32)) == 0.0


def test_rms_constant() -> None:
    assert audio.rms(np.full(1000, 0.5, dtype=np.float32)) == pytest.approx(0.5)


def test_rms_sine_amplitude_over_sqrt2() -> None:
    t = np.arange(16000, dtype=np.float32) / 16000
    sig = (0.2 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)
    assert audio.rms(sig) == pytest.approx(0.2 / np.sqrt(2), rel=1e-3)


# ---------------------------------------------------------------------------
# normalize


def _sine(amplitude: float, n: int = 16000) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / 16000
    return (amplitude * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


def test_normalize_empty_is_noop() -> None:
    out = audio.normalize(np.zeros(0, dtype=np.float32))
    assert out.size == 0


def test_normalize_never_amplifies_pure_silence() -> None:
    silence = np.zeros(4000, dtype=np.float32)
    out = audio.normalize(silence)
    assert np.array_equal(out, silence)


def test_normalize_boosts_quiet_speech_to_target() -> None:
    quiet = _sine(0.005)  # rms ~0.0035, gain ~14 < max_gain
    out = audio.normalize(quiet, target_rms=0.05, max_gain=30.0)
    assert audio.rms(out) == pytest.approx(0.05, rel=1e-2)
    assert out.dtype == np.float32


def test_normalize_caps_gain() -> None:
    very_quiet = _sine(0.001)  # would need gain ~70; capped at 30
    in_rms = audio.rms(very_quiet)
    out = audio.normalize(very_quiet, target_rms=0.05, max_gain=30.0)
    assert audio.rms(out) == pytest.approx(in_rms * 30.0, rel=1e-2)
    assert audio.rms(out) < 0.05


def test_normalize_does_not_attenuate_loud_audio() -> None:
    loud = _sine(0.5)  # rms ~0.35 > target
    out = audio.normalize(loud, target_rms=0.05)
    assert np.array_equal(out, loud)


def test_normalize_clips_to_unit_range() -> None:
    # Mostly quiet with one big spike: gain pushes the spike past 1.0.
    sig = np.full(1000, 0.001, dtype=np.float32)
    sig[500] = 0.5
    out = audio.normalize(sig, target_rms=0.05, max_gain=30.0)
    assert float(np.max(out)) <= 1.0
    assert float(np.min(out)) >= -1.0
    assert float(np.max(out)) == pytest.approx(1.0)  # spike actually clipped


# ---------------------------------------------------------------------------
# resample


def test_resample_identity_when_rates_equal() -> None:
    sig = _sine(0.3, n=1234)
    out = audio.resample(sig, 16000, 16000)
    assert np.array_equal(out, sig)


def test_resample_empty() -> None:
    out = audio.resample(np.zeros(0, dtype=np.float32), 44100, 16000)
    assert out.size == 0


def test_resample_exact_length_downsample() -> None:
    out = audio.resample(np.ones(1600, dtype=np.float32), 16000, 8000)
    assert out.size == 800


def test_resample_exact_length_upsample() -> None:
    n = 1000
    out = audio.resample(np.ones(n, dtype=np.float32), 16000, 44100)
    assert out.size == round(n * 44100 / 16000)


def test_resample_preserves_constant() -> None:
    out = audio.resample(np.full(1600, 0.5, dtype=np.float32), 16000, 8000)
    assert np.allclose(out, 0.5, atol=1e-6)


def test_resample_linear_ramp_stays_a_ramp() -> None:
    sr_from, sr_to = 16000, 8000
    n = 1600
    ramp = (np.arange(n, dtype=np.float32) / sr_from).astype(np.float32)  # f(t) = t
    out = audio.resample(ramp, sr_from, sr_to)
    expected = np.arange(out.size, dtype=np.float32) / sr_to
    # last sample may be clamped to the source's final value
    assert np.allclose(out[:-1], expected[:-1], atol=1e-5)
    assert out.dtype == np.float32


def test_resample_rejects_bad_rates() -> None:
    with pytest.raises(ValueError, match="positive"):
        audio.resample(np.ones(10, dtype=np.float32), 0, 16000)
    with pytest.raises(ValueError, match="positive"):
        audio.resample(np.ones(10, dtype=np.float32), 16000, -1)


# ---------------------------------------------------------------------------
# SilenceDetector


def _chunk(level: float, n: int = 1600) -> np.ndarray:  # 0.1 s at 16 kHz
    return np.full(n, level, dtype=np.float32)


def test_silence_detector_never_triggers_without_speech() -> None:
    det = audio.SilenceDetector(silence_s=0.3, rms_gate=0.01, sample_rate=16000)
    for _ in range(50):
        det.feed(_chunk(0.0))
    assert det.triggered is False


def test_silence_detector_triggers_after_trailing_silence() -> None:
    det = audio.SilenceDetector(silence_s=0.3, rms_gate=0.01, sample_rate=16000)
    det.feed(_chunk(0.1))  # speech
    det.feed(_chunk(0.0))  # 0.1 s silence
    assert det.triggered is False
    det.feed(_chunk(0.0))  # 0.2 s
    assert det.triggered is False
    det.feed(_chunk(0.0))  # 0.3 s >= silence_s
    assert det.triggered is True


def test_silence_detector_speech_resets_counter() -> None:
    det = audio.SilenceDetector(silence_s=0.3, rms_gate=0.01, sample_rate=16000)
    det.feed(_chunk(0.1))
    det.feed(_chunk(0.0))
    det.feed(_chunk(0.0))  # 0.2 s of silence banked
    det.feed(_chunk(0.1))  # speech again: counter resets
    det.feed(_chunk(0.0))
    det.feed(_chunk(0.0))
    assert det.triggered is False  # only 0.2 s since last speech
    det.feed(_chunk(0.0))
    assert det.triggered is True


def test_silence_detector_gate_boundary() -> None:
    # rms exactly at the gate counts as speech (silence means rms < gate).
    # 0.25 is exactly representable in float32, so the boundary is exact.
    det = audio.SilenceDetector(silence_s=0.1, rms_gate=0.25, sample_rate=16000)
    det.feed(_chunk(0.25))  # at gate -> speech
    det.feed(_chunk(0.2))  # below gate -> silence (0.1 s)
    assert det.triggered is True


def test_silence_detector_stays_triggered() -> None:
    det = audio.SilenceDetector(silence_s=0.1, rms_gate=0.01, sample_rate=16000)
    det.feed(_chunk(0.1))
    det.feed(_chunk(0.0))
    assert det.triggered is True
    det.feed(_chunk(0.1))  # late speech doesn't untrigger
    assert det.triggered is True


def test_silence_detector_ignores_empty_chunks() -> None:
    det = audio.SilenceDetector(silence_s=0.1, rms_gate=0.01, sample_rate=16000)
    det.feed(_chunk(0.1))
    det.feed(np.zeros(0, dtype=np.float32))
    assert det.triggered is False


def test_silence_detector_speech_rms_defaults_to_gate() -> None:
    det = audio.SilenceDetector(silence_s=0.1, rms_gate=0.01, sample_rate=16000)
    assert det.speech_rms == det.rms_gate
    det.feed(_chunk(0.001))  # below the gate: not speech, never arms
    det.feed(_chunk(0.0))
    assert det.triggered is False


def test_silence_detector_speech_rms_arms_for_whispers() -> None:
    # Whisper-quiet speech (rms 0.001) is far below rms_gate (0.005), but a
    # speech_rms derived from the normalize rescue potential still arms the
    # detector so auto-stop works for quiet speakers.
    speech_rms = 0.005 / audio.NORMALIZE_MAX_GAIN
    det = audio.SilenceDetector(
        silence_s=0.1, rms_gate=0.005, sample_rate=16000, speech_rms=speech_rms
    )
    det.feed(_chunk(0.001))  # whisper: above speech_rms, below rms_gate
    det.feed(_chunk(0.0))  # 0.1 s trailing silence
    assert det.triggered is True


def test_silence_detector_whisper_speech_resets_counter() -> None:
    speech_rms = 0.005 / audio.NORMALIZE_MAX_GAIN
    det = audio.SilenceDetector(
        silence_s=0.2, rms_gate=0.005, sample_rate=16000, speech_rms=speech_rms
    )
    det.feed(_chunk(0.001))  # whisper speech
    det.feed(_chunk(0.0))  # 0.1 s silence banked
    det.feed(_chunk(0.001))  # whisper again: counter resets
    det.feed(_chunk(0.0))
    assert det.triggered is False  # only 0.1 s since last (quiet) speech
    det.feed(_chunk(0.0))
    assert det.triggered is True


# ---------------------------------------------------------------------------
# Recorder


def test_recorder_start_opens_stream_with_expected_args(fake_sd) -> None:
    rec = audio.Recorder(samplerate=16000)
    rec.start()
    assert rec.is_active()
    (stream,) = fake_sd.streams
    assert stream.started
    assert stream.kwargs["samplerate"] == 16000
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "float32"
    assert "device" not in stream.kwargs  # default: let sounddevice pick


def test_recorder_passes_device_name(fake_sd) -> None:
    audio.Recorder(16000, device="USB Mic").start()
    assert fake_sd.streams[0].kwargs["device"] == "USB Mic"


def test_recorder_numeric_string_device_becomes_index(fake_sd) -> None:
    audio.Recorder(16000, device="3").start()
    assert fake_sd.streams[0].kwargs["device"] == 3


def test_recorder_int_device_passed_through(fake_sd) -> None:
    audio.Recorder(16000, device=5).start()
    assert fake_sd.streams[0].kwargs["device"] == 5


def test_recorder_blank_device_means_default(fake_sd) -> None:
    audio.Recorder(16000, device="  ").start()
    assert "device" not in fake_sd.streams[0].kwargs


def test_recorder_callback_appends_flattened_chunks(fake_sd) -> None:
    rec = audio.Recorder(16000)
    rec.start()
    cb = fake_sd.streams[0].callback
    cb(np.full((4, 1), 0.25, dtype=np.float32), 4, None, None)
    cb(np.full((2, 1), -0.5, dtype=np.float32), 2, None, None)
    assert len(rec.chunks) == 2  # live-readable while recording
    assert rec.chunks[0].shape == (4,)
    out, _dur = rec.stop()
    assert out.shape == (6,)
    assert out.dtype == np.float32
    assert np.allclose(out[:4], 0.25) and np.allclose(out[4:], -0.5)


def test_recorder_callback_logs_status_to_stderr(fake_sd, capsys: pytest.CaptureFixture) -> None:
    rec = audio.Recorder(16000)
    rec.start()
    fake_sd.streams[0].callback(np.zeros((4, 1), dtype=np.float32), 4, None, "input overflow")
    assert "input overflow" in capsys.readouterr().err


def test_recorder_stop_duration_math(fake_sd, monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 100.0}
    monkeypatch.setattr(audio.time, "monotonic", lambda: now["t"])
    rec = audio.Recorder(16000)
    rec.start()
    now["t"] = 100.2
    assert rec.elapsed_ms() == pytest.approx(200.0)
    now["t"] = 100.75
    fake_sd.streams[0].callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
    out, dur = rec.stop()
    assert dur == pytest.approx(750.0)
    assert out.size == 8
    assert fake_sd.streams[0].stopped and fake_sd.streams[0].closed
    assert not rec.is_active()
    assert rec.elapsed_ms() == 0.0


def test_recorder_stop_without_start_is_safe(fake_sd) -> None:
    out, dur = audio.Recorder(16000).stop()
    assert out.size == 0
    assert dur == 0.0


def test_recorder_stop_with_no_chunks_returns_empty(fake_sd) -> None:
    rec = audio.Recorder(16000)
    rec.start()
    out, dur = rec.stop()
    assert out.size == 0
    assert out.dtype == np.float32
    assert dur >= 0.0


def test_recorder_start_resets_previous_chunks(fake_sd) -> None:
    rec = audio.Recorder(16000)
    rec.start()
    fake_sd.streams[0].callback(np.ones((4, 1), dtype=np.float32), 4, None, None)
    rec.stop()
    rec.start()
    assert rec.chunks == []


def test_recorder_double_start_stops_previous_stream(
    fake_sd, capsys: pytest.CaptureFixture
) -> None:
    rec = audio.Recorder(16000)
    rec.start()
    first = fake_sd.streams[0]
    rec.start()  # double start must never leak the live stream
    assert first.stopped and first.closed
    assert "already recording" in capsys.readouterr().err  # warning logged
    assert rec.is_active()
    second = fake_sd.streams[1]
    assert second.started
    # The second take is clean: only the new stream's chunks are collected.
    second.callback(np.full((4, 1), 0.5, dtype=np.float32), 4, None, None)
    out, _dur = rec.stop()
    assert out.size == 4
    assert second.stopped and second.closed


def test_recorder_double_start_survives_broken_old_stream(fake_sd) -> None:
    rec = audio.Recorder(16000)
    rec.start()

    def explode() -> None:
        raise RuntimeError("PaErrorCode -9988")

    fake_sd.streams[0].stop = explode  # old stream errors on stop
    rec.start()  # must not raise; new stream still comes up
    assert rec.is_active()
    assert len(fake_sd.streams) == 2
    assert fake_sd.streams[1].started


def test_recorder_start_failure_closes_stream_and_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = make_fake_sd(fail_start=True)
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    rec = audio.Recorder(16000, device="USB Mic")
    with pytest.raises(RuntimeError) as ei:
        rec.start()
    assert mod.streams[0].closed  # no leaked half-open stream
    assert not rec.is_active()
    msg = str(ei.value)
    assert "USB Mic" in msg
    assert "python -m sounddevice" in msg  # fix suggestion present


def test_recorder_open_failure_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = make_fake_sd(fail_init=True)
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    rec = audio.Recorder(16000)
    with pytest.raises(RuntimeError, match="microphone"):
        rec.start()
    assert not rec.is_active()


# ---------------------------------------------------------------------------
# play_beep


def test_play_beep_synthesizes_start_tone(fake_sd) -> None:
    audio.play_beep("start")
    ((data, sr),) = fake_sd.played
    assert sr == 22050
    assert data.size == int(22050 * 60 / 1000)  # 60 ms at 22.05 kHz
    assert data.dtype == np.float32
    assert float(np.max(np.abs(data))) <= 0.25 + 1e-6
    assert float(np.max(np.abs(data))) > 0.1  # actually audible
    assert abs(float(data[0])) < 1e-6  # attack envelope starts at zero


@pytest.mark.parametrize(
    ("kind", "duration_ms"),
    [("start", 60), ("stop", 40), ("cancel", 100), ("error", 180)],
)
def test_play_beep_kind_durations(fake_sd, kind: str, duration_ms: int) -> None:
    audio.play_beep(kind)
    ((data, sr),) = fake_sd.played
    assert sr == 22050
    assert data.size == int(22050 * duration_ms / 1000)


def test_play_beep_disabled_plays_nothing(fake_sd) -> None:
    audio.play_beep("start", enabled=False)
    assert fake_sd.played == []


def test_play_beep_unknown_kind_plays_nothing(fake_sd) -> None:
    audio.play_beep("kaboom")
    assert fake_sd.played == []


def test_play_beep_survives_missing_sounddevice(monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes `import sounddevice` raise ImportError.
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    audio.play_beep("start")  # must not raise


def test_play_beep_survives_play_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = make_fake_sd(fail_play=True)
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    audio.play_beep("error")  # must not raise
