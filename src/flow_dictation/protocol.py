"""Wire protocol shared by RemoteEngine (client) and `flow serve` (server).

Audio travels as 16-bit PCM mono WAV bytes (base64 inside JSON bodies) or
as raw little-endian int16 frames on the streaming websocket. Both sides of
the wire use the helpers here so encode/decode never drift apart.
"""

from __future__ import annotations

import base64
import io
import wave

import numpy as np

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765

# Websocket text-frame message types ({"type": ...}).
MSG_START = "start"
MSG_PARTIAL = "partial"
MSG_FINALIZE = "finalize"
MSG_POLISH = "polish"
MSG_FINAL = "final"
MSG_CANCEL = "cancel"
MSG_ERROR = "error"


def float_to_pcm16(audio: np.ndarray) -> np.ndarray:
    """float32 [-1, 1] -> little-endian int16 samples (clipped, rounded)."""
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    return np.rint(np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")


def pcm16_to_float(pcm: np.ndarray | bytes | bytearray | memoryview) -> np.ndarray:
    """Little-endian int16 samples (or raw bytes) -> float32 in [-1, 1]."""
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        data = bytes(pcm)
        if len(data) % 2:  # tolerate a truncated trailing byte
            data = data[:-1]
        arr = np.frombuffer(data, dtype="<i2")
    else:
        arr = np.asarray(pcm, dtype="<i2").reshape(-1)
    return np.clip(arr.astype(np.float32) / 32767.0, -1.0, 1.0)


def encode_wav(audio: np.ndarray, sample_rate: int) -> bytes:
    """Encode float32 mono audio as 16-bit PCM WAV bytes."""
    pcm = float_to_pcm16(audio)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes to (float32 mono audio in [-1, 1], sample_rate).

    Accepts any sample rate. Multi-channel input is downmixed to mono.
    Raises ValueError with a helpful message for non-WAV payloads and for
    sample widths other than 16-bit PCM.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            sample_rate = w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(
            f"not a valid WAV payload: {e} — encode audio with protocol.encode_wav()"
        ) from e
    if width != 2:
        raise ValueError(
            f"unsupported WAV sample width: {width * 8}-bit — the flow protocol uses "
            "16-bit PCM; re-encode with protocol.encode_wav()"
        )
    pcm = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        usable = (pcm.size // channels) * channels
        pcm = pcm[:usable].reshape(-1, channels).mean(axis=1)
    audio = np.clip(np.asarray(pcm).astype(np.float32) / 32767.0, -1.0, 1.0)
    return audio, sample_rate


def to_b64(data: bytes) -> str:
    """Bytes -> base64 ASCII string (for JSON transport)."""
    return base64.b64encode(data).decode("ascii")


def from_b64(text: str) -> bytes:
    """Base64 string -> bytes. Raises ValueError on malformed input."""
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as e:
        raise ValueError(f"invalid base64 audio payload: {e}") from e
