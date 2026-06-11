"""protocol.py: WAV codec, PCM helpers, base64 helpers."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from voicisst import protocol


def test_constants() -> None:
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.DEFAULT_PORT == 8765


def test_audio_cap_constants() -> None:
    # Shared request/stream size cap: int16 samples <-> bytes must agree,
    # and the cap must hold at least 120 s of 16 kHz stereo int16 audio.
    assert protocol.MAX_AUDIO_BYTES == 32 * 1024 * 1024
    assert protocol.MAX_AUDIO_SAMPLES * 2 == protocol.MAX_AUDIO_BYTES
    assert protocol.MAX_AUDIO_BYTES >= 120 * 16000 * 2 * 2


@pytest.mark.parametrize("sr", [16000, 22050, 48000])
@pytest.mark.parametrize("n", [160, 1601, 4801])
def test_wav_round_trip(sr: int, n: int) -> None:
    rng = np.random.default_rng(seed=n + sr)
    audio = rng.uniform(-0.9, 0.9, n).astype(np.float32)
    data = protocol.encode_wav(audio, sr)
    assert data[:4] == b"RIFF"
    decoded, out_sr = protocol.decode_wav(data)
    assert out_sr == sr
    assert decoded.dtype == np.float32
    assert decoded.shape == (n,)
    # int16 quantization: at most one LSB of error.
    assert float(np.max(np.abs(decoded - audio))) <= 1.0 / 32767 + 1e-6


def test_wav_round_trip_empty() -> None:
    decoded, sr = protocol.decode_wav(protocol.encode_wav(np.zeros(0, np.float32), 16000))
    assert decoded.size == 0
    assert sr == 16000


def test_encode_clips_out_of_range() -> None:
    audio = np.array([1.5, -1.5, 0.0], dtype=np.float32)
    decoded, _ = protocol.decode_wav(protocol.encode_wav(audio, 16000))
    assert decoded[0] == pytest.approx(1.0, abs=1e-4)
    assert decoded[1] == pytest.approx(-1.0, abs=1e-4)
    assert decoded[2] == pytest.approx(0.0, abs=1e-6)
    assert float(np.max(np.abs(decoded))) <= 1.0


def test_decode_rejects_non_16_bit() -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)  # 8-bit
        w.setframerate(16000)
        w.writeframes(bytes(64))
    with pytest.raises(ValueError, match="16-bit"):
        protocol.decode_wav(buf.getvalue())


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="WAV"):
        protocol.decode_wav(b"this is definitely not a wav file")
    with pytest.raises(ValueError, match="WAV"):
        protocol.decode_wav(b"")


def test_decode_downmixes_stereo() -> None:
    left = (np.ones(100) * 16000).astype("<i2")
    right = (np.ones(100) * -16000).astype("<i2")
    interleaved = np.empty(200, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(interleaved.tobytes())
    decoded, sr = protocol.decode_wav(buf.getvalue())
    assert sr == 44100
    assert decoded.shape == (100,)
    assert np.allclose(decoded, 0.0, atol=1e-4)


def test_pcm16_helpers_round_trip() -> None:
    audio = np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0], dtype=np.float32)
    pcm = protocol.float_to_pcm16(audio)
    assert pcm.dtype == np.dtype("<i2")
    assert pcm[3] == 32767
    assert pcm[4] == -32767
    assert pcm[5] == 32767  # clipped
    back = protocol.pcm16_to_float(pcm.tobytes())
    assert back.dtype == np.float32
    assert np.max(np.abs(back[:5] - audio[:5])) <= 1.0 / 32767 + 1e-6


def test_pcm16_to_float_tolerates_odd_byte_count() -> None:
    pcm = protocol.float_to_pcm16(np.zeros(4, dtype=np.float32)).tobytes() + b"\x01"
    assert protocol.pcm16_to_float(pcm).shape == (4,)


def test_b64_helpers() -> None:
    data = bytes(range(256))
    assert protocol.from_b64(protocol.to_b64(data)) == data
    with pytest.raises(ValueError, match="base64"):
        protocol.from_b64("!!! not base64 !!!")
