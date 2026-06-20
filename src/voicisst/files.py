"""Audio-file transcription pipeline.

This is the non-realtime sibling of dictation: decode a file, transcribe it
in bounded chunks, then polish and return text instead of injecting it into
the focused app.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from . import audio as audio_mod
from .engine.base import EngineError
from .textproc import apply_replacements, sanitize

if TYPE_CHECKING:
    from .config import Config
    from .engine.base import Engine

FILE_SAMPLE_RATE = 16000
DEFAULT_CHUNK_SECONDS = 120.0
MIN_CHUNK_SECONDS = 1.0
MAX_CHUNK_SECONDS = 600.0


@dataclass(frozen=True)
class AudioFileInfo:
    path: Path
    sample_rate: int
    duration_s: float
    chunks: int


@dataclass(frozen=True)
class FileTranscript:
    raw: str
    text: str
    duration_s: float
    chunks: int
    sample_rate: int = FILE_SAMPLE_RATE


ProgressCallback = Callable[[dict[str, Any]], None]


def collect_file_vocab(cfg: Config) -> str:
    """Dictionary context for file transcription.

    Unlike live dictation this intentionally ignores the current primary
    selection, because processing a file should not depend on whatever text
    happened to be highlighted in another app.
    """
    terms: list[str] = []
    for word in cfg.dictionary.words:
        word = str(word).strip()
        if word:
            terms.append(word)
    path = cfg.dictionary.resolved_path()
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    terms.append(line)
    except OSError:
        pass
    return ", ".join(terms)


def clamp_chunk_seconds(seconds: float | int | str | None) -> float:
    try:
        value = float(DEFAULT_CHUNK_SECONDS if seconds is None else seconds)
    except (TypeError, ValueError):
        value = DEFAULT_CHUNK_SECONDS
    return min(max(value, MIN_CHUNK_SECONDS), MAX_CHUNK_SECONDS)


def probe_audio_file(path: Path | str) -> AudioFileInfo:
    """Best-effort file metadata for UI progress and error messages."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise EngineError(f"audio file not found: {path}", hint="choose an existing file")
    try:
        import av  # type: ignore

        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise EngineError(
                    f"{path.name} does not contain an audio stream",
                    hint="choose an audio recording such as .m4a, .wav, .flac, or .aiff",
                )
            sample_rate = int(getattr(stream, "rate", 0) or FILE_SAMPLE_RATE)
            duration_s = 0.0
            if stream.duration is not None and stream.time_base is not None:
                duration_s = float(stream.duration * stream.time_base)
            elif container.duration:
                duration_s = float(container.duration / 1_000_000)
            chunks = int(np.ceil(duration_s / DEFAULT_CHUNK_SECONDS)) if duration_s > 0 else 0
            return AudioFileInfo(
                path=path,
                sample_rate=sample_rate,
                duration_s=duration_s,
                chunks=chunks,
            )
    except ImportError:
        pass
    except EngineError:
        raise
    except Exception:
        pass

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            cmd = [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                sample_rate = int(lines[0]) if lines and lines[0].isdigit() else FILE_SAMPLE_RATE
                duration_s = float(lines[1]) if len(lines) > 1 else 0.0
                chunks = int(np.ceil(duration_s / DEFAULT_CHUNK_SECONDS)) if duration_s > 0 else 0
                return AudioFileInfo(
                    path=path,
                    sample_rate=sample_rate,
                    duration_s=duration_s,
                    chunks=chunks,
                )
        except Exception:
            pass

    return AudioFileInfo(path=path, sample_rate=FILE_SAMPLE_RATE, duration_s=0.0, chunks=0)


def iter_audio_file_chunks(
    path: Path | str,
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    sample_rate: int = FILE_SAMPLE_RATE,
) -> Iterator[np.ndarray]:
    """Yield mono float32 chunks decoded from `path`.

    M4A/AAC and most other media containers are supported through PyAV or an
    `ffmpeg` executable. WAV also works without either dependency.
    """
    path = Path(path).expanduser()
    chunk_seconds = clamp_chunk_seconds(chunk_seconds)
    chunk_samples = max(1, int(round(sample_rate * chunk_seconds)))
    if not path.is_file():
        raise EngineError(f"audio file not found: {path}", hint="choose an existing file")

    pyav_error: Exception | None = None
    try:
        yield from _iter_chunks_pyav(path, chunk_samples, sample_rate)
        return
    except ImportError:
        pass
    except EngineError:
        raise
    except Exception as e:
        pyav_error = e
    else:
        pyav_error = None

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            yield from _iter_chunks_ffmpeg(path, chunk_samples, sample_rate, ffmpeg)
            return
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(
                f"could not decode {path.name}: {e}",
                hint="check that the file is a readable audio recording",
            ) from e

    if path.suffix.lower() == ".wav":
        yield from _iter_chunks_wav(path, chunk_samples, sample_rate)
        return

    detail = f": {pyav_error}" if pyav_error is not None else ""
    raise EngineError(
        f"could not decode {path.name}{detail}",
        hint="M4A/MP3/media files need PyAV (`pip install av`) or ffmpeg on PATH",
    )


def process_audio_file(
    path: Path | str,
    cfg: Config,
    engine: Engine,
    *,
    language: str | None = None,
    polish: bool = True,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    progress: ProgressCallback | None = None,
    vocab: str | None = None,
) -> FileTranscript:
    """Decode, transcribe and optionally polish an audio file."""
    path = Path(path).expanduser()
    chunk_seconds = clamp_chunk_seconds(chunk_seconds)
    language = cfg.whisper.language_or_none() if language is None else language
    vocab = collect_file_vocab(cfg) if vocab is None else vocab
    raw_parts: list[str] = []
    total_samples = 0
    chunk_count = 0

    if progress is not None:
        progress({"status": "decoding", "path": str(path), "chunk": 0})
    for chunk in iter_audio_file_chunks(path, chunk_seconds=chunk_seconds):
        chunk_count += 1
        total_samples += int(chunk.size)
        if cfg.audio.normalize:
            chunk = audio_mod.normalize(chunk)
        if progress is not None:
            progress(
                {
                    "status": "transcribing",
                    "chunk": chunk_count,
                    "seconds": total_samples / FILE_SAMPLE_RATE,
                }
            )
        raw = engine.transcribe(
            chunk,
            FILE_SAMPLE_RATE,
            language=language,
            vocab=vocab,
        ).strip()
        if raw:
            raw_parts.append(sanitize(raw))

    raw_text = "\n\n".join(raw_parts).strip()
    cleaned = raw_text
    if polish and cfg.polish.enabled and raw_text:
        if progress is not None:
            progress({"status": "polishing", "chunk": chunk_count})
        cleaned = engine.polish(raw_text, language=language, vocab=vocab).strip() or raw_text
    cleaned = apply_replacements(sanitize(cleaned), cfg.replacements).strip()
    duration_s = total_samples / FILE_SAMPLE_RATE if total_samples else 0.0
    if progress is not None:
        progress(
            {
                "status": "done",
                "chunk": chunk_count,
                "seconds": duration_s,
                "chars": len(cleaned),
            }
        )
    return FileTranscript(
        raw=raw_text,
        text=cleaned,
        duration_s=duration_s,
        chunks=chunk_count,
    )


def _chunk_arrays(frames: Iterator[np.ndarray], chunk_samples: int) -> Iterator[np.ndarray]:
    pending = np.zeros(0, dtype=np.float32)
    for frame in frames:
        arr = np.asarray(frame, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            continue
        pending = np.concatenate([pending, arr]) if pending.size else arr
        while pending.size >= chunk_samples:
            yield pending[:chunk_samples].astype(np.float32, copy=False)
            pending = pending[chunk_samples:]
    if pending.size:
        yield pending.astype(np.float32, copy=False)


def _iter_chunks_pyav(
    path: Path, chunk_samples: int, sample_rate: int
) -> Iterator[np.ndarray]:
    import av  # type: ignore

    try:
        container = av.open(str(path))
    except Exception as e:
        raise EngineError(
            f"could not open {path.name}: {e}",
            hint="check that the file is a readable audio recording",
        ) from e
    with container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise EngineError(
                f"{path.name} does not contain an audio stream",
                hint="choose an audio recording such as .m4a, .wav, .flac, or .aiff",
            )
        resampler = av.audio.resampler.AudioResampler(
            format="flt",
            layout="mono",
            rate=sample_rate,
        )

        def frames() -> Iterator[np.ndarray]:
            for frame in container.decode(stream):
                for out in _resample_frame(resampler, frame):
                    yield _frame_to_float_mono(out)
            for out in _resample_frame(resampler, None):
                yield _frame_to_float_mono(out)

        yield from _chunk_arrays(frames(), chunk_samples)


def _resample_frame(resampler: Any, frame: Any) -> list[Any]:
    out = resampler.resample(frame)
    if out is None:
        return []
    if isinstance(out, list):
        return out
    return [out]


def _frame_to_float_mono(frame: Any) -> np.ndarray:
    arr = np.asarray(frame.to_ndarray())
    channels = int(len(getattr(frame.layout, "channels", []) or []) or 1)
    if arr.ndim == 2:
        if arr.shape[0] == channels:
            arr = arr.mean(axis=0)
        elif arr.shape[-1] == channels:
            arr = arr.mean(axis=-1)
        else:
            arr = arr.reshape(-1)
    elif arr.ndim > 2:
        arr = arr.reshape(-1)
    if np.issubdtype(arr.dtype, np.floating):
        out = arr.astype(np.float32, copy=False)
    elif np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        scale = float(max(abs(info.min), info.max))
        out = arr.astype(np.float32) / np.float32(scale)
    else:
        out = arr.astype(np.float32)
    return np.clip(out.reshape(-1), -1.0, 1.0).astype(np.float32, copy=False)


def _iter_chunks_ffmpeg(
    path: Path,
    chunk_samples: int,
    sample_rate: int,
    ffmpeg: str,
) -> Iterator[np.ndarray]:
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stderr is not None
    read_size = chunk_samples * 4
    try:
        while True:
            data = proc.stdout.read(read_size)
            if not data:
                break
            usable = len(data) - (len(data) % 4)
            if usable:
                yield np.frombuffer(data[:usable], dtype="<f4").astype(np.float32)
        stderr = proc.stderr.read().decode("utf-8", "replace").strip()
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if code != 0:
        raise EngineError(
            f"ffmpeg could not decode {path.name}: {stderr or f'exit {code}'}",
            hint="check that the file is a readable audio recording",
        )


def _iter_chunks_wav(
    path: Path, chunk_samples: int, sample_rate: int
) -> Iterator[np.ndarray]:
    data = path.read_bytes()
    from .protocol import decode_wav

    audio, sr = decode_wav(data)
    if sr != sample_rate:
        audio = audio_mod.resample(audio, sr, sample_rate)
    yield from _chunk_arrays(iter([audio]), chunk_samples)
