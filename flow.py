#!/usr/bin/env python3
"""
flow.py — hold-to-talk dictation for Lucy.

Hold the Menu key, speak, release. Whisper transcribes locally, Ollama
(qwen3.5:4b) cleans it up, the result is pasted (or typed) into the
focused window.

Config via env vars (all optional):
    WHISPER_MODEL        default "base.en"
    WHISPER_BACKEND      "auto" | "faster" | "openai"  (default "auto")
    WHISPER_DEVICE       "auto" | "cuda" | "cpu"       (default "auto")
    WHISPER_COMPUTE      faster-whisper compute_type   (default "float16" on cuda)
    OLLAMA_MODEL         default "qwen3.5:4b"
    OLLAMA_URL           default "http://localhost:11434"
    POLISH_ENABLED       default "1"
    POLISH_KEEP_ALIVE    default "30m"
    VRAM_UNLOAD_BELOW_MB default "1024"     (poll nvidia-smi every 60s; if
                                              free VRAM drops below this and
                                              the polish model is loaded,
                                              unload it. 0 disables.)
    POLISH_TIMEOUT       default "20"
    HOTKEY_NAMES         default "KEY_COMPOSE,KEY_MENU"  (comma list)
    MIN_RECORD_MS        default "300"
    MAX_RECORD_MS        default "120000"   (2 min hard cap)
    MUTED_RMS            default "0.00001"  (below this -> "mic muted?")
    RMS_GATE             default "0.005"
    SAMPLE_RATE          default "16000"
    OUTPUT_MODE          default "paste"    ("paste" | "type")
    STREAM               default "0"        (1 = type live while speaking,
                                              then replace with polished
                                              text on release)
    STREAM_TICK_MS       default "600"      (how often to re-transcribe)
    KEY_DELAY_MS         default "1"        (ydotool inter-key delay; lower
                                              = faster typing/backspace)
    NEWLINE_MODE         default "shift-enter"  ("shift-enter" | "enter")
                                              shift-enter sends Shift+Enter
                                              for each \n in the output —
                                              chat apps (Claude, Slack,
                                              Discord) treat plain Enter as
                                              submit.
    TERMINAL_CLASSES     default "kitty,alacritty,foot,wezterm,konsole,
                                  org.gnome.Terminal,xterm,ptyxis"
    BEEP                 default "1"        (paplay short tones on start/stop)
"""
from __future__ import annotations

import json
import os
import queue
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests


# ---------------------------------------------------------------------------
# Config

def _envbool(name: str, default: str) -> bool:
    return os.environ.get(name, default) not in ("0", "false", "False", "no", "")


@dataclass(frozen=True)
class Config:
    whisper_model: str = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    whisper_backend: str = os.environ.get("WHISPER_BACKEND", "auto").lower()
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "auto").lower()
    whisper_compute: str = os.environ.get("WHISPER_COMPUTE", "")
    ollama_model: str = os.environ.get(
        "OLLAMA_MODEL", "hf.co/unsloth/Qwen3-30B-A3B-GGUF:UD-Q3_K_XL"
    )
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    polish_enabled: bool = _envbool("POLISH_ENABLED", "1")
    polish_keep_alive: str = os.environ.get("POLISH_KEEP_ALIVE", "30m")
    polish_num_ctx: int = int(os.environ.get("POLISH_NUM_CTX", "8192"))
    polish_num_predict: int = int(os.environ.get("POLISH_NUM_PREDICT", "2048"))
    polish_think: bool = _envbool("POLISH_THINK", "1")
    # Below this length, skip thinking (short inputs don't benefit from it
    # and it adds 10-20s of latency).
    polish_think_min_chars: int = int(
        os.environ.get("POLISH_THINK_MIN_CHARS", "100")
    )
    polish_num_gpu: int = int(os.environ.get("POLISH_NUM_GPU", "36"))
    vram_unload_below_mb: int = int(os.environ.get("VRAM_UNLOAD_BELOW_MB", "1024"))
    polish_timeout: float = float(os.environ.get("POLISH_TIMEOUT", "60"))
    hotkey_names: tuple = tuple(
        n.strip() for n in os.environ.get(
            "HOTKEY_NAMES", "KEY_COMPOSE,KEY_MENU"
        ).split(",") if n.strip()
    )
    min_record_ms: int = int(os.environ.get("MIN_RECORD_MS", "1000"))
    max_record_ms: int = int(os.environ.get("MAX_RECORD_MS", "120000"))
    muted_rms: float = float(os.environ.get("MUTED_RMS", "0.00001"))
    rms_gate: float = float(os.environ.get("RMS_GATE", "0.005"))
    sample_rate: int = int(os.environ.get("SAMPLE_RATE", "16000"))
    output_mode: str = os.environ.get("OUTPUT_MODE", "paste").lower()
    terminal_classes: tuple = tuple(
        c.strip() for c in os.environ.get(
            "TERMINAL_CLASSES",
            "kitty,alacritty,foot,wezterm,konsole,org.gnome.Terminal,xterm,ptyxis",
        ).split(",") if c.strip()
    )
    beep: bool = _envbool("BEEP", "1")
    stream: bool = _envbool("STREAM", "0")
    stream_tick_ms: int = int(os.environ.get("STREAM_TICK_MS", "600"))
    key_delay_ms: int = int(os.environ.get("KEY_DELAY_MS", "0"))
    key_hold_ms: int = int(os.environ.get("KEY_HOLD_MS", "0"))
    newline_mode: str = os.environ.get("NEWLINE_MODE", "shift-enter").lower()


CFG = Config()

POLISH_SYSTEM_PROMPT = """\
You rework raw dictated speech into clear, well-formatted written text.

Your job is NOT just to clean fillers — it is to FULLY FORMAT and REWORK the text so it reads clearly on the page. Reorder, regroup, and rephrase as needed for clarity. Preserve every distinct point the speaker made, but do not feel bound to their exact word order or sentence boundaries when a clearer arrangement is available.

Output ONLY the reworked text. No preamble, no quotes, no explanation.

CORE RULES:
1. Kill all filler: um, uh, er, ah, mm, like, you know, sort of, kind of, basically, literally, honestly, I mean, well (filler), so (filler), anyway, right, just (filler). Ruthless.
2. On self-correction, keep ONLY the final version. Drop the abandoned attempt AND the correction marker (actually, no wait, sorry, I mean, scratch that, or rather).
3. Collapse stutters and repeats: "the the file" -> "the file"; "I think I think" -> "I think".
4. Fix grammar (agreement, tense, articles) and rephrase awkward or convoluted spoken constructions into clear written sentences. Keep the speaker's voice and vocabulary, but don't preserve clumsy phrasing for its own sake. Don't formalise casual speech beyond what clarity requires.
5. Capitalise sentences. Expand voice commands: comma -> ","  period / full stop -> "."  question mark -> "?"  exclamation point -> "!"  new line -> newline  new paragraph -> double newline.
6. Two or more items in succession -> NUMBERED Markdown list. Be aggressive — when in doubt, list it. Triggers include: "one X two Y three Z", "first X second Y third Z", "first is X second is Y", "X then Y then Z", "also X also Y", or any colon-lead-in followed by 2+ items. **Lead-in prose and trailing prose around the list stay as prose; ONLY the enumerated section becomes the list.** Drop the spoken numbers/ordinals/connectors ("one", "first", "first is", "then", "also"). Items with a short title get formatted as "1. Title - description." with a literal hyphen-space.
7. Fully restructure for clarity. Break long run-on thoughts into separate sentences or paragraphs. Group related ideas together even if the speaker scattered them. Pull out lists, headings, and paragraph breaks whenever they make the content easier to read. Use Markdown formatting (numbered lists, bullet lists, bold for emphasis on key terms, paragraph breaks) freely wherever it improves clarity.
8. Preserve every distinct point and every concrete detail. You may reorder, regroup, and rephrase for clarity, but never drop content, never summarise away substance, and never invent new facts.

EXAMPLES:

Input:  um so basically i was thinking we should you know maybe ship the auth refactor next sprint actually no wait this sprint
Output: We should ship the auth refactor this sprint.

Input:  send the email to bob actually no send it to alice
Output: Send the email to Alice.

Input:  i think i think we should do the the refactor first
Output: I think we should do the refactor first.

Input:  three things to do today comma one finish the report two call the dentist three email tim
Output: Three things to do today:
1. Finish the report.
2. Call the dentist.
3. Email Tim.

Input:  crazy product ideas colon one self watering plant shoes sneakers with built in planters two mood color changing wallpaper smart wallpaper that shifts color based on your mood three portable nap pod backpack backpack unfolds into a private soundproof nap cocoon
Output: Crazy product ideas:
1. Self-watering plant shoes - sneakers with built-in planters.
2. Mood color changing wallpaper - smart wallpaper that shifts color based on your mood.
3. Portable nap pod backpack - backpack unfolds into a private, soundproof nap cocoon.

Input:  first we ship the bug fix then we write the test then we tag the release
Output: 1. Ship the bug fix.
2. Write the test.
3. Tag the release.

Input:  so i'm working out some crazy product ideas first is a self-watering plant shoes sneakers with built-in planters i think this is going to be excellent second is mood color changing wallpaper wallpaper that changes color based on your mood third is portable nap pod backpack it's a backpack that unfolds into a private soundproof nap cocoon and i want you all to try these things out
Output: I'm working out some crazy product ideas:
1. Self-watering plant shoes - sneakers with built-in planters. I think this is going to be excellent.
2. Mood color changing wallpaper - wallpaper that changes color based on your mood.
3. Portable nap pod backpack - a backpack that unfolds into a private, soundproof nap cocoon.

I want you all to try these things out.

Input:  ideas for the weekend first is hiking second is the movies third is brunch
Output: Ideas for the weekend:
1. Hiking.
2. The movies.
3. Brunch.

Input:  meet at three pm sorry i mean four pm
Output: Meet at 4 pm.

Input:  i need to pick up eggs milk and bread on the way home
Output: I need to pick up eggs, milk, and bread on the way home.

Input:  hey can you grab milk on your way home thanks
Output: Hey, can you grab milk on your way home? Thanks.

Input:  tell claude to look at the file
Output: Tell Claude to look at the file.

Input:  um so basically i was thinking we should you know maybe ship it next week
Output: We should maybe ship it next week.

Input:  okay so basically what i think we need to do is um maybe pull the migration out into a separate step you know because right now its all in one transaction
Output: We should pull the migration out into a separate step because right now it's all in one transaction.

Input:  so i was thinking we could do the auth refactor next sprint actually no wait we should do it this sprint because security is breathing down our neck
Output: We should do the auth refactor this sprint because security is breathing down our neck.

Input:  we need two things one a database and two a load balancer
Output: We need two things:
1. A database.
2. A load balancer.

Input:  i want to add caching also rate limiting also a circuit breaker
Output: 1. Caching.
2. Rate limiting.
3. A circuit breaker.

CRITICAL: Numbered list items ALWAYS go on their own lines, separated by literal newlines. Never put "1. X 2. Y" on one line."""


# ---------------------------------------------------------------------------
# Feedback helpers

_BEEP_CACHE: dict[str, Path] = {}


def _ensure_beep(freq_hz: int, duration_ms: int) -> Path | None:
    """Generate a short sine WAV the first time we need it, cache on disk."""
    key = f"{freq_hz}_{duration_ms}"
    cached = _BEEP_CACHE.get(key)
    if cached and cached.exists():
        return cached
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    out = runtime / f"flow-beep-{key}.wav"
    sr = 22050
    n = int(sr * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / sr
    env = np.minimum(1.0, np.minimum(t * 200, (duration_ms / 1000 - t) * 200))
    samples = (np.sin(2 * np.pi * freq_hz * t) * env * 0.25 * 32767).astype(np.int16)
    try:
        with out.open("wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + samples.nbytes))
            f.write(b"WAVEfmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
            f.write(b"data")
            f.write(struct.pack("<I", samples.nbytes))
            f.write(samples.tobytes())
        _BEEP_CACHE[key] = out
        return out
    except OSError:
        return None


def beep(kind: str) -> None:
    if not CFG.beep:
        return
    spec = {
        "start": (880, 60),
        "stop": (660, 40),
        "cancel": (220, 100),
        "error": (180, 180),
    }.get(kind)
    if not spec:
        return
    wav = _ensure_beep(*spec)
    if not wav:
        return
    player = shutil.which("paplay") or shutil.which("pw-play")
    if not player:
        return
    try:
        subprocess.Popen(
            [player, str(wav)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def notify(summary: str, body: str = "", urgency: str = "low") -> None:
    # GNOME stacked the notify-send bubbles indefinitely — disabled.
    # Kept as a stderr log for debugging.
    print(f"[{summary}] {body}", file=sys.stderr)


def live(summary: str, body: str = "", *, dismiss: bool = False) -> None:
    # Disabled — see notify(). All in-flight state is shown inline via the
    # streaming typer ([Processing (1/2)…] / [Processing (2/2)…]).
    if not dismiss:
        print(f"[live] {summary} {body}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Audio capture

class Recorder:
    def __init__(self, samplerate: int = CFG.sample_rate):
        import sounddevice as sd
        self.sd = sd
        self.samplerate = samplerate
        self.chunks: list[np.ndarray] = []
        self.stream = None
        self.start_ts = 0.0
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"audio status: {status}", file=sys.stderr)
        self.chunks.append(indata.copy().flatten())

    def start(self) -> None:
        self.chunks = []
        self.start_ts = time.monotonic()
        stream = self.sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        self.stream = stream

    def stop(self) -> tuple[np.ndarray, float]:
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


# ---------------------------------------------------------------------------
# Transcription

def _pick_backend() -> str:
    """Return 'faster' or 'openai' based on CFG.whisper_backend + availability."""
    forced = CFG.whisper_backend
    if forced == "faster":
        return "faster"
    if forced == "openai":
        return "openai"
    # auto
    try:
        import faster_whisper  # noqa: F401
        return "faster"
    except ImportError:
        return "openai"


def _pick_device() -> tuple[str, str]:
    """Return (device, compute_type) for faster-whisper."""
    dev = CFG.whisper_device
    if dev == "auto":
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                dev = "cuda"
            else:
                dev = "cpu"
        except Exception:
            dev = "cpu"
    compute = CFG.whisper_compute or ("float16" if dev == "cuda" else "int8")
    return dev, compute


class Transcriber:
    def __init__(self, model_name: str = CFG.whisper_model):
        self.backend = _pick_backend()
        if self.backend == "faster":
            from faster_whisper import WhisperModel
            device, compute = _pick_device()
            print(
                f"loading faster-whisper {model_name} on {device}/{compute}",
                file=sys.stderr,
            )
            self.model = WhisperModel(model_name, device=device, compute_type=compute)
            self.device = device
        else:
            print(f"loading openai-whisper {model_name}", file=sys.stderr)
            import whisper
            self.model = whisper.load_model(model_name)
            self.device = "cpu"
        print(f"whisper ready (backend={self.backend})", file=sys.stderr)

    def transcribe(self, audio: np.ndarray, vocab: str = "") -> str:
        if audio.size == 0:
            return ""
        if self.backend == "faster":
            segs, _info = self.model.transcribe(
                audio, language="en", vad_filter=False, beam_size=5,
                initial_prompt=vocab or None,
            )
            return "".join(s.text for s in segs).strip()
        kwargs = {"language": "en", "fp16": False}
        if vocab:
            kwargs["initial_prompt"] = vocab
        result = self.model.transcribe(audio, **kwargs)
        return (result.get("text") or "").strip()


# ---------------------------------------------------------------------------
# LLM polish

def _strip_quotes(s: str) -> str:
    if len(s) < 2:
        return s
    if s[0] == s[-1] and s[0] in ('"', "'") and s[0] not in s[1:-1]:
        return s[1:-1].strip()
    return s


def _gpu_free_mb() -> int | None:
    """Free VRAM on GPU 0 in MiB, or None if nvidia-smi unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=2, text=True,
        )
        if r.returncode == 0:
            return int(r.stdout.strip().splitlines()[0])
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return None


class OllamaKeepalive:
    """Watchdog that unloads the polish model early when VRAM gets contested.

    Ollama already evicts after `keep_alive` (we set 30m per request). This
    layer adds: if another process needs more VRAM than is free, dump the
    polish model so it can have it. The model reloads on next polish call.
    """

    def __init__(self, threshold_mb: int):
        self.threshold_mb = threshold_mb
        self._loaded = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.threshold_mb <= 0 or not shutil.which("nvidia-smi"):
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def note_use(self) -> None:
        self._loaded = True

    def _loop(self) -> None:
        # Tick every 60s. Skip if model isn't loaded (note_use never called).
        while not self._stop.wait(60):
            if not self._loaded:
                continue
            free = _gpu_free_mb()
            if free is None or free >= self.threshold_mb:
                continue
            try:
                requests.post(
                    f"{CFG.ollama_url}/api/generate",
                    json={"model": CFG.ollama_model, "keep_alive": 0},
                    timeout=5,
                )
                print(
                    f"unloaded {CFG.ollama_model} (free VRAM {free}MB < "
                    f"{self.threshold_mb}MB threshold)",
                    file=sys.stderr,
                )
                self._loaded = False
            except requests.RequestException as e:
                print(f"unload request failed: {e}", file=sys.stderr)


KEEPALIVE = OllamaKeepalive(CFG.vram_unload_below_mb)

# Set by the worker while polish_stream is actively iterating. The input
# thread only honors Backspace-cancel while this is set, so out-of-window
# backspace remains a normal edit key.
POLISHING = threading.Event()
# Set by the input thread when Backspace is pressed during polishing.
# Consumed by the worker to abort the polish and fall back to raw whisper.
CANCEL_POLISH = threading.Event()


def polish_warm() -> None:
    """Prime the polish path: same system prompt and options as the real
    polish() call so Ollama's KV cache for the system prompt is hot before
    the first user utterance.
    """
    if not CFG.polish_enabled:
        return
    try:
        requests.post(
            f"{CFG.ollama_url}/api/generate",
            json={
                "model": CFG.ollama_model,
                "system": POLISH_SYSTEM_PROMPT if CFG.polish_think
                          else POLISH_SYSTEM_PROMPT + "\n/no_think",
                "prompt": "hello" if CFG.polish_think else "hello /no_think",
                "stream": False,
                "keep_alive": CFG.polish_keep_alive,
                "options": {
                    "temperature": 0.1,
                    "num_predict": CFG.polish_num_predict,
                    "num_ctx": CFG.polish_num_ctx,
                    "num_gpu": CFG.polish_num_gpu,
                },
            },
            timeout=120,
        )
    except requests.RequestException:
        pass


def _ollama_error_hint(e: Exception) -> str:
    s = str(e)
    if isinstance(e, requests.ConnectionError):
        return "ollama not running — try `systemctl status ollama`"
    if isinstance(e, requests.Timeout):
        return "ollama timeout — model may be cold; raising POLISH_TIMEOUT may help"
    if "404" in s:
        return f"model not pulled — `ollama pull {CFG.ollama_model}`"
    return s


def polish(text: str) -> str:
    if not CFG.polish_enabled or not text:
        return text
    try:
        # NB: 'think' API field doesn't apply to HF-pulled GGUFs (no template
        # marker). The model still emits <think>…</think> by default; we
        # strip below. To force off, append /no_think to system+prompt.
        # Hybrid: only let the model think on longer inputs where it pays off.
        use_think = (CFG.polish_think
                     and len(text) >= CFG.polish_think_min_chars)
        payload = {
            "model": CFG.ollama_model,
            "system": POLISH_SYSTEM_PROMPT if use_think
                      else POLISH_SYSTEM_PROMPT + "\n/no_think",
            "prompt": text if use_think else text + " /no_think",
            "stream": False,
            "keep_alive": CFG.polish_keep_alive,
            "options": {
                "temperature": 0.1,
                "num_predict": CFG.polish_num_predict,
                "num_ctx": CFG.polish_num_ctx,
                "num_gpu": CFG.polish_num_gpu,
            },
        }
        r = requests.post(
            f"{CFG.ollama_url}/api/generate",
            json=payload,
            timeout=CFG.polish_timeout,
        )
        r.raise_for_status()
        cleaned = (r.json().get("response") or "").strip()
        # Strip any leaked <think>...</think> block from the response.
        if "<think>" in cleaned:
            import re as _re
            cleaned = _re.sub(r"<think>.*?</think>", "", cleaned,
                              flags=_re.DOTALL).strip()
        cleaned = _strip_quotes(cleaned)
        KEEPALIVE.note_use()
        return cleaned or text
    except requests.RequestException as e:
        notify("polish failed", _ollama_error_hint(e), urgency="normal")
        return text


def _strip_think(s: str) -> str:
    """Remove complete <think>…</think> blocks. If a <think> tag is open
    without a closing tag, return empty string (still thinking)."""
    if "<think>" in s and "</think>" not in s:
        return ""
    if "<think>" in s:
        import re as _re
        s = _re.sub(r"<think>.*?</think>", "", s, flags=_re.DOTALL)
    return s.strip()


def polish_stream(text: str):
    """Generator: yields the cleaned text-so-far as the model streams.

    Final yield is the complete polished text. On any failure, yields the
    original text once and returns.
    """
    if not CFG.polish_enabled or not text:
        yield text
        return
    use_think = (CFG.polish_think
                 and len(text) >= CFG.polish_think_min_chars)
    payload = {
        "model": CFG.ollama_model,
        "system": POLISH_SYSTEM_PROMPT if use_think
                  else POLISH_SYSTEM_PROMPT + "\n/no_think",
        "prompt": text if use_think else text + " /no_think",
        "stream": True,
        "keep_alive": CFG.polish_keep_alive,
        "options": {
            "temperature": 0.1,
            "num_predict": CFG.polish_num_predict,
            "num_ctx": CFG.polish_num_ctx,
            "num_gpu": CFG.polish_num_gpu,
        },
    }
    try:
        with requests.post(
            f"{CFG.ollama_url}/api/generate",
            json=payload,
            timeout=CFG.polish_timeout,
            stream=True,
        ) as r:
            r.raise_for_status()
            accumulated = ""
            last_visible = ""
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tok = chunk.get("response", "")
                if tok:
                    accumulated += tok
                    visible = _strip_think(accumulated)
                    if visible and visible != last_visible:
                        last_visible = visible
                        yield _strip_quotes(visible)
                if chunk.get("done"):
                    final = _strip_quotes(_strip_think(accumulated)) or text
                    if final != last_visible:
                        yield final
                    KEEPALIVE.note_use()
                    return
    except requests.RequestException as e:
        notify("polish failed", _ollama_error_hint(e), urgency="normal")
        yield text


# ---------------------------------------------------------------------------
# Output safety

def sanitize(text: str) -> str:
    """Strip control + escape characters that could reprogram a terminal."""
    return "".join(
        c for c in text
        if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 0x7f)
    )


def focused_window_class() -> str | None:
    """Best-effort focused-window class detection. Returns None if unknown."""
    # GNOME Wayland: there is no portable public API. Try the Shell DBus call
    # which works only with the Window Calls or similar extension; falls back
    # to nothing if absent.
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Shell",
             "--object-path", "/org/gnome/Shell/Extensions/WindowsExt",
             "--method", "org.gnome.Shell.Extensions.WindowsExt.FocusClass"],
            capture_output=True, timeout=0.5, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip().strip("(),'\"")
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    # Sway
    if shutil.which("swaymsg"):
        try:
            r = subprocess.run(
                ["swaymsg", "-t", "get_tree"],
                capture_output=True, timeout=0.5, text=True,
            )
            if r.returncode == 0:
                tree = json.loads(r.stdout)
                def find_focused(node):
                    if node.get("focused"):
                        return node.get("app_id") or (node.get("window_properties") or {}).get("class")
                    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
                        hit = find_focused(child)
                        if hit:
                            return hit
                    return None
                return find_focused(tree)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass
    # Hyprland
    if shutil.which("hyprctl"):
        try:
            r = subprocess.run(
                ["hyprctl", "-j", "activewindow"],
                capture_output=True, timeout=0.5, text=True,
            )
            if r.returncode == 0:
                return (json.loads(r.stdout) or {}).get("class")
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass
    return None


def _looks_like_terminal(cls: str | None) -> bool:
    if not cls:
        return False
    low = cls.lower()
    return any(t.lower() in low for t in CFG.terminal_classes)


_YDOTOOL_FLAG_CACHE: dict[tuple[str, str], bool] = {}


def _ydotool_supports(subcmd: str, flag: str) -> bool:
    """Cached check: does `ydotool <subcmd> --help` mention `<flag>`?"""
    key = (subcmd, flag)
    if key in _YDOTOOL_FLAG_CACHE:
        return _YDOTOOL_FLAG_CACHE[key]
    supported = False
    if shutil.which("ydotool"):
        try:
            r = subprocess.run(
                ["ydotool", subcmd, "--help"],
                capture_output=True, timeout=3, text=True,
            )
            supported = flag in (r.stdout + r.stderr)
        except (subprocess.SubprocessError, OSError):
            supported = False
    _YDOTOOL_FLAG_CACHE[key] = supported
    return supported


def _ydotool_timing_flags(subcmd: str) -> list[str]:
    """Build --key-delay / --key-hold args, omitting unsupported ones."""
    args: list[str] = []
    if _ydotool_supports(subcmd, "--key-delay"):
        args += ["--key-delay", str(CFG.key_delay_ms)]
    if CFG.key_hold_ms > 0 and _ydotool_supports(subcmd, "--key-hold"):
        args += ["--key-hold", str(CFG.key_hold_ms)]
    return args


def _ydotool_paste() -> bool:
    """Run ydotool to press Ctrl+Shift+V. Returns True on success."""
    if not shutil.which("ydotool"):
        return False
    # 29=LCTRL, 42=LSHIFT, 47=V. Press, release in reverse.
    try:
        subprocess.run(
            ["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
            check=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _ydotool_type_raw(text: str) -> bool:
    """Type a literal segment with no special newline handling."""
    if not shutil.which("ydotool") or not text:
        return not text
    try:
        subprocess.run(
            ["ydotool", "type", *_ydotool_timing_flags("type"), "--", text],
            check=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


# evdev keycode 28 = KEY_ENTER, 42 = KEY_LEFTSHIFT, 14 = KEY_BACKSPACE.
def _ydotool_shift_enter() -> bool:
    if not shutil.which("ydotool"):
        return False
    try:
        subprocess.run(
            ["ydotool", "key", *_ydotool_timing_flags("key"),
             "42:1", "28:1", "28:0", "42:0"],
            check=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _ydotool_type(text: str) -> bool:
    """Type text, optionally translating \\n into Shift+Enter."""
    if not text:
        return True
    if CFG.newline_mode != "shift-enter" or "\n" not in text:
        return _ydotool_type_raw(text)
    parts = text.split("\n")
    for i, part in enumerate(parts):
        if part and not _ydotool_type_raw(part):
            return False
        if i < len(parts) - 1:
            if not _ydotool_shift_enter():
                return False
    return True


def _ydotool_backspace(n: int) -> bool:
    """Send exactly n backspace key events. Never sends more than n."""
    if n <= 0 or not shutil.which("ydotool"):
        return False
    sent = 0
    while sent < n:
        batch = min(n - sent, 128)
        args = []
        for _ in range(batch):
            args.extend(["14:1", "14:0"])
        try:
            subprocess.run(
                ["ydotool", "key", *_ydotool_timing_flags("key"), *args],
                check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False
        sent += batch
    return True


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            return i
        i += 1
    return i


class StreamingTyper:
    """Live-types the partial transcript while the user is still speaking.

    Invariant: `last_typed` is always exactly the string we believe we've
    sent to the focused window. Every backspace decrements its length;
    every type extends it. We never backspace more than len(last_typed).
    """

    def __init__(self, recorder: Recorder, transcriber: "Transcriber"):
        self.recorder = recorder
        self.transcriber = transcriber
        self.tick_s = CFG.stream_tick_ms / 1000.0
        self.last_typed = ""
        # Number of trailing chars of last_typed that are a "suffix" — a
        # temporary indicator we can swap or clear without touching the
        # base text in front of it.
        self._suffix_len = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Held while transcribing OR while emitting key events. A new tick
        # that finds the lock taken simply skips — prevents queueing.
        self._lock = threading.Lock()

    def start(self) -> None:
        self.last_typed = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        """Stop the loop, return whatever string we last typed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # Take the lock once more so any in-flight tick is finished and
        # last_typed is settled before the caller reads it.
        with self._lock:
            return self.last_typed

    def replace_with(self, target: str) -> None:
        """Atomically swap last_typed for `target` in the focused window.

        Deletes exactly len(last_typed) characters, then types `target`.
        Updates last_typed in lockstep so a partial failure leaves the
        counter consistent with what's on screen.
        """
        with self._lock:
            self._replace_locked(target)

    def _replace_locked(self, target: str) -> None:
        # Reduce to common prefix to minimise visible churn.
        prefix = _common_prefix_len(self.last_typed, target)
        to_delete = len(self.last_typed) - prefix
        if to_delete > 0:
            if _ydotool_backspace(to_delete):
                self.last_typed = self.last_typed[:prefix]
            else:
                # Backspace failed — bail without touching last_typed.
                return
        tail = target[prefix:]
        if tail:
            if _ydotool_type(tail):
                self.last_typed = self.last_typed + tail
        # any replace_with-style operation invalidates suffix tracking
        self._suffix_len = 0

    def erase_all(self) -> None:
        """Delete exactly the streamed text. Used on cancel / silence."""
        with self._lock:
            n = len(self.last_typed)
            if n and _ydotool_backspace(n):
                self.last_typed = ""
                self._suffix_len = 0

    def set_suffix(self, suffix: str) -> None:
        """Swap the trailing suffix in place. Base text (everything before
        the current suffix) is untouched. Passing '' clears the suffix.
        """
        with self._lock:
            old_n = self._suffix_len
            if old_n:
                if _ydotool_backspace(old_n):
                    self.last_typed = self.last_typed[:-old_n]
                    self._suffix_len = 0
                else:
                    return
            if suffix:
                if _ydotool_type(suffix):
                    self.last_typed += suffix
                    self._suffix_len = len(suffix)

    def _loop(self) -> None:
        sr = self.recorder.samplerate
        min_samples = int(sr * 0.5)  # need ≥0.5s of audio
        while not self._stop.wait(self.tick_s):
            if not self._lock.acquire(blocking=False):
                continue  # previous tick still running
            try:
                chunks = list(self.recorder.chunks)
                if not chunks:
                    continue
                try:
                    audio = np.concatenate(chunks).astype(np.float32)
                except ValueError:
                    continue
                if audio.size < min_samples:
                    continue
                try:
                    raw = self.transcriber.transcribe(audio).strip()
                except Exception as e:
                    print(f"stream transcribe error: {e}", file=sys.stderr)
                    continue
                self._replace_locked(raw)
            finally:
                self._lock.release()


def _wl_paste_primary() -> str:
    """Read the X PRIMARY selection (what's currently highlighted). Returns
    empty string if nothing selected or wl-paste unavailable."""
    if not shutil.which("wl-paste"):
        return ""
    try:
        r = subprocess.run(
            ["wl-paste", "--primary", "--no-newline"],
            capture_output=True, timeout=1, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _clear_selection() -> None:
    """Best-effort: tap Escape to deselect in most apps."""
    if shutil.which("ydotool"):
        try:
            # 1 = KEY_ESC
            subprocess.run(
                ["ydotool", "key", *_ydotool_timing_flags("key"), "1:1", "1:0"],
                check=False, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _wl_copy(text: str) -> bool:
    if not shutil.which("wl-copy"):
        return False
    try:
        subprocess.run(["wl-copy"], input=text.encode(), check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def deliver(text: str) -> None:
    text = sanitize(text)
    if not text:
        return
    cls = focused_window_class()
    is_term = _looks_like_terminal(cls)

    if is_term:
        if _wl_copy(text):
            notify(
                "terminal detected",
                f"({cls}) — text copied, press Ctrl+Shift+V to paste",
                urgency="normal",
            )
        else:
            notify("terminal detected; clipboard unavailable", str(cls), urgency="critical")
        return

    mode = CFG.output_mode
    if mode == "paste":
        if _wl_copy(text) and _ydotool_paste():
            return
        # clipboard worked but paste failed — leave it on clipboard
        notify("paste failed", "text is on clipboard — press Ctrl+V", urgency="normal")
        return

    # type mode
    if _ydotool_type(text):
        return
    if _wl_copy(text):
        notify(
            "ydotool unavailable",
            "text copied — press Ctrl+V (or `systemctl --user start ydotoold`)",
            urgency="normal",
        )
        return
    notify("delivery failed", "text printed to stderr", urgency="critical")
    print(f"OUTPUT: {text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Hotkey discovery

def find_hotkey_devices(key_names: tuple):
    """Return (devices, set-of-keycodes-of-interest)."""
    import evdev
    from evdev import ecodes
    wanted_codes = set()
    for name in key_names:
        code = ecodes.ecodes.get(name)
        if code is None:
            print(f"warning: unknown key name {name}", file=sys.stderr)
            continue
        wanted_codes.add(code)
    if not wanted_codes:
        raise SystemExit(f"no valid keys in HOTKEY_NAMES={key_names}")
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError) as e:
            print(f"skip {path}: {e}", file=sys.stderr)
            continue
        # Skip ydotool's own virtual keyboard. We use it to type/backspace,
        # so listening on it would feedback-loop our generated Backspace
        # events into the polish-cancel branch.
        name = (dev.name or "").lower()
        if "ydotool" in name:
            print(f"skip {path}: {dev.name} (our own virtual kbd)",
                  file=sys.stderr)
            dev.close()
            continue
        caps = set(dev.capabilities().get(ecodes.EV_KEY, []))
        if caps & wanted_codes:
            devices.append(dev)
        else:
            dev.close()
    return devices, wanted_codes


# ---------------------------------------------------------------------------
# Main loop

# Event types posted from the input thread to the worker thread.
EV_START = "start"
EV_STOP = "stop"
EV_CANCEL = "cancel"
EV_QUIT = "quit"


def worker_loop(q: "queue.Queue", recorder: Recorder, transcriber: Transcriber):
    streamer: StreamingTyper | None = (
        StreamingTyper(recorder, transcriber) if CFG.stream else None
    )
    pending_vocab = ""  # captured PRIMARY selection at press time

    while True:
        ev = q.get()
        if ev == EV_QUIT:
            if streamer is not None:
                streamer.stop()
            return
        if ev == EV_START:
            try:
                pending_vocab = _wl_paste_primary()
                if pending_vocab:
                    _clear_selection()
                    live("◉ Listening…", f"vocab: {pending_vocab[:60]}")
                else:
                    live("◉ Listening…", "")
                recorder.start()
                beep("start")
                if streamer is not None:
                    streamer.start()
            except Exception as e:
                notify("recorder failed", str(e), urgency="critical")
                beep("error")
            continue
        if ev in (EV_STOP, EV_CANCEL):
            # Stop the streaming loop BEFORE the final transcribe so the
            # shared whisper model isn't called from two threads at once.
            streamed_len = 0
            if streamer is not None:
                streamed = streamer.stop()
                streamed_len = len(streamed)
            try:
                audio, dur_ms = recorder.stop()
            except Exception as e:
                notify("recorder stop failed", str(e), urgency="critical")
                live("", dismiss=True)
                if streamer is not None:
                    streamer.erase_all()
                continue
            if ev == EV_CANCEL:
                beep("cancel")
                live("✕ Cancelled", "")
                if streamer is not None:
                    streamer.erase_all()
                continue
            beep("stop")

            def _reject(reason: str | None = None) -> None:
                # used for too-short / silent / muted / empty-transcript
                if streamer is not None:
                    streamer.erase_all()
                if reason:
                    notify(reason[0], reason[1])
                live("", dismiss=True)

            if dur_ms < CFG.min_record_ms:
                _reject(); continue
            if audio.size == 0:
                _reject(); continue
            rms = float(np.sqrt(np.mean(audio * audio)))
            if rms < CFG.muted_rms:
                _reject(("mic muted?",
                        f"rms={rms:.6f} — check pavucontrol / pipewire"))
                continue
            if rms < CFG.rms_gate:
                _reject(); continue
            # Append inline " [Processing (1/2)…]" as a suffix after the
            # whisper text — base text stays intact.
            if streamer is not None:
                streamer.set_suffix(" [Processing (1/2)…]")
            live("◌ Transcribing…", f"{dur_ms/1000:.1f}s")
            t0 = time.monotonic()
            raw = transcriber.transcribe(audio, vocab=pending_vocab)
            pending_vocab = ""
            if not raw:
                if streamer is not None:
                    streamer.set_suffix("")
                _reject(); continue
            t1 = time.monotonic()
            live("✎ Polishing…", raw[:120])
            if streamer is not None:
                streamer.set_suffix(" [Processing (2/2)…]")
            cleaned = raw
            cancelled = False
            if streamer is not None:
                # Stream polish output token-by-token. Backspace during
                # this window cancels — falls back to raw whisper.
                CANCEL_POLISH.clear()
                POLISHING.set()
                gen = polish_stream(raw)
                try:
                    for chunk in gen:
                        if CANCEL_POLISH.is_set():
                            cancelled = True
                            break
                        streamer.replace_with(sanitize(chunk))
                        cleaned = chunk
                finally:
                    gen.close()
                    POLISHING.clear()
                if cancelled:
                    streamer.replace_with(sanitize(raw))
                    cleaned = raw
                    notify("polish cancelled", "using raw transcript")
            else:
                cleaned = sanitize(polish(raw))
                deliver(cleaned)
            t2 = time.monotonic()
            live("✓ Done" + (" (raw)" if cancelled else ""), cleaned[:120])
            print(
                f"transcribe={t1-t0:.2f}s polish={t2-t1:.2f}s "
                f"streamed={streamed_len}ch cancelled={cancelled} "
                f"raw={raw[:60]!r} -> {cleaned[:60]!r}",
                file=sys.stderr,
            )


def run_loop():
    import evdev
    from evdev import ecodes

    notify("flow starting", f"loading {CFG.whisper_model}…")
    transcriber = Transcriber()
    recorder = Recorder()
    polish_warm()
    KEEPALIVE.start()

    devices, wanted_codes = find_hotkey_devices(CFG.hotkey_names)
    if not devices:
        notify(
            "no input device with hotkey",
            f"{CFG.hotkey_names} — are you in 'input' group? `groups`",
            urgency="critical",
        )
        raise SystemExit(2)
    notify(
        "flow ready",
        f"hold {'/'.join(CFG.hotkey_names)} on {len(devices)} keyboards",
    )

    q: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=worker_loop, args=(q, recorder, transcriber), daemon=True,
    )
    worker.start()

    fd_to_dev = {dev.fd: dev for dev in devices}
    # We only honor the *first* device whose key went down; ignore others
    # until that device's key comes up. Prevents multi-keyboard races.
    holder_fd: int | None = None
    last_press_ts = 0.0

    try:
        while fd_to_dev:
            try:
                r, _, _ = select.select(list(fd_to_dev.keys()), [], [], 1.0)
            except KeyboardInterrupt:
                raise

            # max-record watchdog
            if (holder_fd is not None
                    and recorder.is_active()
                    and recorder.elapsed_ms() > CFG.max_record_ms):
                notify(
                    "max record hit",
                    f"{CFG.max_record_ms/1000:.0f}s cap — stopping",
                    urgency="normal",
                )
                q.put(EV_STOP)
                holder_fd = None

            for fd in r:
                dev = fd_to_dev.get(fd)
                if dev is None:
                    continue
                try:
                    events = list(dev.read())
                except BlockingIOError:
                    continue
                except OSError as e:
                    print(f"device {dev.path} dead ({e}); removing", file=sys.stderr)
                    try:
                        dev.close()
                    except Exception:
                        pass
                    fd_to_dev.pop(fd, None)
                    if holder_fd == fd:
                        q.put(EV_CANCEL)
                        holder_fd = None
                    if not fd_to_dev:
                        notify("no keyboards left", "exiting", urgency="critical")
                    continue
                for event in events:
                    if event.type != ecodes.EV_KEY:
                        continue
                    # Backspace pressed while polish is running -> cancel.
                    # Only honored inside the POLISHING window so out-of-
                    # window backspaces remain normal edit keystrokes.
                    if (event.code == ecodes.KEY_BACKSPACE
                            and event.value == 1
                            and POLISHING.is_set()):
                        CANCEL_POLISH.set()
                        continue
                    if event.code not in wanted_codes:
                        continue
                    if event.value == 1:  # down
                        if holder_fd is None:
                            holder_fd = fd
                            last_press_ts = time.monotonic()
                            q.put(EV_START)
                    elif event.value == 0:  # up
                        if holder_fd == fd:
                            holder_fd = None
                            q.put(EV_STOP)
                    # value == 2 is autorepeat, ignore
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        if holder_fd is not None:
            q.put(EV_CANCEL)
        q.put(EV_QUIT)
        KEEPALIVE.stop()
        worker.join(timeout=5)
        for dev in fd_to_dev.values():
            try:
                dev.close()
            except Exception:
                pass
        if recorder.is_active():
            try:
                recorder.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Selftest

def selftest():
    ok = True
    captured = {}

    def step(name: str, fn, *, requires_prior_ok: bool = False):
        nonlocal ok
        if requires_prior_ok and not ok:
            print(f"  {name}… SKIP (earlier step failed)", file=sys.stderr)
            return
        sys.stderr.write(f"  {name}… ")
        sys.stderr.flush()
        try:
            fn()
            print("PASS", file=sys.stderr)
        except Exception as e:
            ok = False
            print(f"FAIL: {e}", file=sys.stderr)

    print("== flow selftest ==", file=sys.stderr)
    print(f"  config: {CFG}", file=sys.stderr)

    def check_evdev():
        devices, _ = find_hotkey_devices(CFG.hotkey_names)
        if not devices:
            raise RuntimeError(
                f"no device exposes any of {CFG.hotkey_names} "
                f"(in 'input' group? `groups | grep input`)"
            )
        for d in devices:
            d.close()
    step("evdev: find devices with hotkey", check_evdev)

    def check_audio():
        rec = Recorder()
        rec.start()
        time.sleep(1.0)
        audio, _ = rec.stop()
        captured["audio"] = audio
        if audio.size == 0:
            raise RuntimeError("got 0 samples from sounddevice")
    step("audio: 1s capture via sounddevice", check_audio)

    def check_whisper():
        t = Transcriber()
        _ = t.transcribe(captured["audio"])
    step(
        f"whisper: load {CFG.whisper_model} + transcribe",
        check_whisper, requires_prior_ok=True,
    )

    def check_ollama():
        r = requests.get(f"{CFG.ollama_url}/api/tags", timeout=3)
        r.raise_for_status()
        tags = [m["name"] for m in r.json().get("models", [])]
        if CFG.ollama_model not in tags:
            raise RuntimeError(f"model {CFG.ollama_model} not in {tags}")
        cleaned = polish("um hello world comma this is a test")
        if not cleaned:
            raise RuntimeError("polish returned empty")
        sys.stderr.write(f"\n      sample: {cleaned!r} ")
    step("ollama: tags + polish round-trip", check_ollama)

    def check_ydotool_binary():
        if not shutil.which("ydotool"):
            raise RuntimeError("ydotool not installed (run setup.sh)")
        subprocess.run(["ydotool", "--help"], check=True, capture_output=True, timeout=3)
    step("ydotool: binary present", check_ydotool_binary)

    def check_ydotoold_socket():
        sock = os.environ.get("YDOTOOL_SOCKET")
        candidates = []
        if sock:
            candidates.append(sock)
        rt = os.environ.get("XDG_RUNTIME_DIR")
        if rt:
            candidates.append(f"{rt}/.ydotool_socket")
        candidates.append("/tmp/.ydotool_socket")
        for c in candidates:
            try:
                st = os.stat(c)
                import stat
                if stat.S_ISSOCK(st.st_mode):
                    sys.stderr.write(f"\n      socket: {c} mode={oct(st.st_mode & 0o777)} ")
                    return
            except OSError:
                continue
        raise RuntimeError(
            f"no ydotool socket found in {candidates} — start ydotoold "
            f"(systemctl --user start ydotoold)"
        )
    step("ydotoold: socket reachable", check_ydotoold_socket)

    def check_wlcopy():
        if not shutil.which("wl-copy"):
            raise RuntimeError("wl-clipboard not installed (run setup.sh)")
    step("wl-copy: binary present", check_wlcopy)

    print("== %s ==" % ("OK" if ok else "FAIL"), file=sys.stderr)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_loop()
