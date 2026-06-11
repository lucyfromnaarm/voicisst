"""faster-whisper transcription wrapper.

`Transcriber` picks a device (CUDA when ctranslate2 sees a GPU), resolves
the configured model name and exposes a single `transcribe()` call.
faster-whisper is the only supported backend; it is imported lazily so the
rest of the package works without the `[local]` extra installed.
"""

from __future__ import annotations

import sys

import numpy as np

from .config import WhisperConfig

WHISPER_SAMPLE_RATE = 16000
# initial_prompt shares the context window with the audio tokens; keep the
# spelling-vocab prompt to roughly 800 chars so it never crowds out speech.
_VOCAB_MAX_CHARS = 800


def _pick_device(cfg: WhisperConfig) -> tuple[str, str]:
    """Return (device, compute_type) for faster-whisper.

    "auto" probes ctranslate2 for CUDA devices and falls back to CPU on
    any failure (no GPU, broken driver, missing libs). Compute type
    defaults to float16 on CUDA and int8 on CPU unless overridden.
    """
    device = (cfg.device or "auto").strip().lower()
    if device == "auto":
        try:
            import ctranslate2  # lazy: ships with faster-whisper

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = cfg.compute or ("float16" if device == "cuda" else "int8")
    return device, compute


class Transcriber:
    """Loads a faster-whisper model once and transcribes float32 mono audio.

    Attributes:
        device: "cuda" or "cpu" (resolved from cfg.device).
        compute: ctranslate2 compute type in use.
        model_name: resolved model (cfg.whisper.model "auto" picks
            large-v3-turbo on CUDA, small on CPU).
    """

    def __init__(self, cfg: WhisperConfig) -> None:
        self.cfg = cfg
        device, compute = _pick_device(cfg)
        self.device = device
        self.compute = compute
        self.model_name = cfg.resolved_model(device)

        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is not installed — local transcription needs it. "
                "Fix: pip install 'voicisst[local]' (or use a remote engine: "
                "voicisst run --server URL)"
            ) from e

        print(
            f"loading faster-whisper {self.model_name} on {device}/{compute}",
            file=sys.stderr,
        )
        try:
            self.model = WhisperModel(self.model_name, device=device, compute_type=compute)
        except Exception as e:
            if device == "cuda":
                hint = (
                    'GPU load failed — set [whisper] device = "cpu" in your config, '
                    "or check the CUDA/cuDNN libraries that ctranslate2 needs"
                )
            else:
                hint = (
                    f"check the model name {self.model_name!r} (e.g. small, large-v3-turbo) "
                    "and that the model can be downloaded (network/disk space)"
                )
            raise RuntimeError(
                f"failed to load whisper model {self.model_name!r} on {device}: {e} — {hint}"
            ) from e
        print(f"whisper ready (model={self.model_name})", file=sys.stderr)

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        language: str | None = None,
        vocab: str = "",
    ) -> str:
        """Transcribe float32 mono audio to text.

        language=None means Whisper auto-detect (100+ languages). `vocab`
        (names/jargon spelling context) is passed as initial_prompt.
        """
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return ""
        if sample_rate != WHISPER_SAMPLE_RATE:
            # Imported here so transcribe.py never depends on audio.py at
            # import time (avoids import-order issues between modules).
            from .audio import resample

            audio = resample(audio, sample_rate, WHISPER_SAMPLE_RATE)
        vocab = vocab.strip()
        initial_prompt = vocab[:_VOCAB_MAX_CHARS] if vocab else None
        segments, _info = self.model.transcribe(
            audio,
            language=language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            initial_prompt=initial_prompt,
        )
        return "".join(s.text for s in segments).strip()
