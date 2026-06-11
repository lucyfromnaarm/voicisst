"""Audio capture, level math, resampling, silence detection, and beeps.

Everything here must work headless: `sounddevice` is imported lazily and
`play_beep` swallows every error so CI machines without audio hardware
never crash.
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np

# kind -> (frequency Hz, duration ms); same tones as the original prototype.
_BEEP_SPECS: dict[str, tuple[int, int]] = {
    "start": (880, 60),
    "stop": (660, 40),
    "cancel": (220, 100),
    "error": (180, 180),
}
_BEEP_SAMPLE_RATE = 22050


def rms(audio: np.ndarray) -> float:
    """Root-mean-square level of a float audio buffer. Empty -> 0.0."""
    if audio.size == 0:
        return 0.0
    a = np.asarray(audio, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a)))


def normalize(
    audio: np.ndarray, target_rms: float = 0.05, max_gain: float = 30.0
) -> np.ndarray:
    """Boost quiet (whispered) speech up to `target_rms`.

    - never amplifies empty buffers or pure silence (rms == 0);
    - never attenuates audio that is already at/above the target;
    - gain is capped at `max_gain`;
    - output is float32 clipped to [-1, 1].
    """
    out = np.asarray(audio, dtype=np.float32)
    if out.size == 0:
        return out
    current = rms(out)
    if current <= 0.0:
        return out  # pure silence: amplifying it only amplifies noise
    gain = min(target_rms / current, max_gain)
    if gain <= 1.0:
        return out  # already loud enough; never attenuate
    return np.clip(out * np.float32(gain), -1.0, 1.0).astype(np.float32)


def resample(audio: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Linear-interpolation resample (no scipy). float32 in, float32 out.

    Output length is exactly round(len(audio) * sr_to / sr_from); identical
    rates return the input untouched.
    """
    if sr_from <= 0 or sr_to <= 0:
        raise ValueError(
            f"sample rates must be positive (got {sr_from} -> {sr_to}); "
            "check [audio] sample_rate in the config"
        )
    out = np.asarray(audio, dtype=np.float32)
    if sr_from == sr_to or out.size == 0:
        return out
    n_out = int(round(out.size * sr_to / sr_from))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    # Sample positions in seconds; np.interp clamps at the edges.
    x_old = np.arange(out.size, dtype=np.float64) / sr_from
    x_new = np.arange(n_out, dtype=np.float64) / sr_to
    return np.interp(x_new, x_old, out.astype(np.float64)).astype(np.float32)


class Recorder:
    """Microphone capture via sounddevice (lazy import).

    `chunks` is intentionally a live, append-only list so streaming code
    (StreamSession feeders) can snapshot it mid-recording, exactly like the
    prototype's StreamingTyper did.
    """

    def __init__(self, samplerate: int = 16000, device: str | int | None = None):
        import sounddevice as sd  # lazy: keep headless imports working

        self.sd = sd
        self.samplerate = samplerate
        self.device = _normalize_device(device)
        self.chunks: list[np.ndarray] = []
        self.stream = None
        self.start_ts = 0.0
        self._lock = threading.Lock()

    def _callback(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        if status:
            print(f"audio status: {status}", file=sys.stderr)
        self.chunks.append(indata.copy().flatten())

    def start(self) -> None:
        with self._lock:
            self.chunks = []
            self.start_ts = time.monotonic()
            kwargs: dict[str, object] = {
                "samplerate": self.samplerate,
                "channels": 1,
                "dtype": "float32",
                "callback": self._callback,
            }
            if self.device is not None:
                kwargs["device"] = self.device
            try:
                stream = self.sd.InputStream(**kwargs)
            except Exception as e:
                raise RuntimeError(self._open_hint(e)) from e
            try:
                stream.start()
            except Exception as e:
                try:
                    stream.close()
                except Exception:
                    pass
                raise RuntimeError(self._open_hint(e)) from e
            self.stream = stream

    def _open_hint(self, e: Exception) -> str:
        where = f" on input device {self.device!r}" if self.device is not None else ""
        return (
            f"could not start audio capture{where}: {e} — is a microphone "
            "connected and not held by another app? List devices with "
            "`python -m sounddevice` and set [audio] input_device in the config."
        )

    def stop(self) -> tuple[np.ndarray, float]:
        """Stop capture; return (float32 mono audio, duration in ms)."""
        with self._lock:
            stream = self.stream
            self.stream = None
            duration_ms = (time.monotonic() - self.start_ts) * 1000.0
            if stream is None:
                return np.zeros(0, dtype=np.float32), 0.0
            try:
                stream.stop()
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
            if not self.chunks:
                return np.zeros(0, dtype=np.float32), duration_ms
            return np.concatenate(self.chunks).astype(np.float32), duration_ms

    def is_active(self) -> bool:
        return self.stream is not None

    def elapsed_ms(self) -> float:
        if self.stream is None:
            return 0.0
        return (time.monotonic() - self.start_ts) * 1000.0


def _normalize_device(device: str | int | None) -> str | int | None:
    """Map AudioConfig.input_device ("" = default) to sounddevice's device arg."""
    if device is None or isinstance(device, int):
        return device
    device = device.strip()
    if not device:
        return None  # "" means system default: don't pass a device at all
    if device.isdigit():
        return int(device)  # "3" is an index, not a name
    return device


class SilenceDetector:
    """Trailing-silence auto-stop: feed float32 chunks, watch `.triggered`.

    A chunk counts as silence when its rms is below `rms_gate`. The
    detector arms only after it has heard speech at least once (so it never
    fires while the user is still drawing breath), and any speech chunk
    resets the trailing-silence counter. Once triggered it stays triggered.
    """

    def __init__(self, silence_s: float, rms_gate: float, sample_rate: int):
        self.silence_s = silence_s
        self.rms_gate = rms_gate
        self.sample_rate = sample_rate
        self.triggered = False
        self._heard_speech = False
        self._silent_samples = 0

    def feed(self, chunk: np.ndarray) -> None:
        if self.triggered or chunk.size == 0:
            return
        if rms(chunk) >= self.rms_gate:
            self._heard_speech = True
            self._silent_samples = 0
            return
        if not self._heard_speech:
            return
        self._silent_samples += int(chunk.size)
        if self._silent_samples / self.sample_rate >= self.silence_s:
            self.triggered = True


def _synth_tone(freq_hz: int, duration_ms: int) -> np.ndarray:
    """Short sine beep with a ~5 ms attack/release envelope (prototype tones)."""
    n = int(_BEEP_SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / _BEEP_SAMPLE_RATE
    envelope = np.minimum(1.0, np.minimum(t * 200, (duration_ms / 1000 - t) * 200))
    return (np.sin(2 * np.pi * freq_hz * t) * envelope * 0.25).astype(np.float32)


def play_beep(kind: str, enabled: bool = True) -> None:
    """Play an audio cue: kind in start/stop/cancel/error.

    Synthesized in memory and played non-blocking via sounddevice. Every
    error (missing module, no output device, ALSA tantrums) is swallowed so
    headless environments never crash over a beep.
    """
    if not enabled:
        return
    spec = _BEEP_SPECS.get(kind)
    if spec is None:
        return
    try:
        import sounddevice as sd  # lazy: optional at runtime, absent on CI

        sd.play(_synth_tone(*spec), _BEEP_SAMPLE_RATE)
    except Exception:
        pass  # a beep is never worth a crash
