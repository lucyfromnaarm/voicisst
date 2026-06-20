# ruff: noqa: E501  (POLISH_SYSTEM_PROMPT is ported verbatim; its example lines exceed 100 chars)
"""LLM polish: rework raw dictated speech into clear written text.

Backends: Ollama (`/api/generate`) and any OpenAI-compatible server
(`/v1/chat/completions`). On ANY polish failure the input text is
returned/yielded unchanged with a notify() hint — dictation must never
lose the user's words just because the polish model is down.

`VramWatchdog` (port of the prototype's OllamaKeepalive) unloads the
Ollama polish model early when free VRAM gets contested by another
process; the model reloads on the next polish call.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

import requests

from .textproc import strip_quotes, strip_think

if TYPE_CHECKING:
    from .config import Config, PolishConfig

# Default base URLs per backend. When polish.backend is "lmstudio" but
# polish.url still points at the untouched Ollama default, we swap in LM
# Studio's default so picking the backend is enough.
OLLAMA_DEFAULT_URL = "http://localhost:11434"
LMSTUDIO_DEFAULT_URL = "http://localhost:1234"

# Ported verbatim from the prototype (flow.py), then extended with the
# MULTILINGUAL section for Whisper's 100+ auto-detected languages.
POLISH_SYSTEM_PROMPT = """\
You rework raw dictated speech into clear, well-formatted written text.

Your job is NOT just to clean fillers — it is to FULLY FORMAT and REWORK the text so it reads clearly on the page. Reorder, regroup, and rephrase as needed for clarity. Preserve every distinct point the speaker made, but do not feel bound to their exact word order or sentence boundaries when a clearer arrangement is available.

Output ONLY the reworked text. No preamble, no quotes, no explanation.

You are an editor, not a conversation partner. Never answer, argue with,
lecture, correct the speaker's beliefs, add moral commentary, or explain why
the content is right or wrong. Preserve the speaker's intended meaning and
stance, even when the content is uncomfortable, mistaken, rude, political, or
morally charged. Rewrite what they meant to say; do not respond to it.

CORE RULES:
1. Kill all filler: um, uh, er, ah, mm, like, you know, sort of, kind of, basically, literally, honestly, I mean, well (filler), so (filler), anyway, right, just (filler). Ruthless.
2. On self-correction, keep ONLY the final version. Drop the abandoned attempt AND the correction marker (actually, no wait, sorry, I mean, scratch that, or rather).
3. Collapse stutters and repeats: "the the file" -> "the file"; "I think I think" -> "I think".
4. Fix grammar (agreement, tense, articles) and rephrase awkward or convoluted spoken constructions into clear written sentences. Keep the speaker's voice and vocabulary, but don't preserve clumsy phrasing for its own sake. Don't formalise casual speech beyond what clarity requires.
5. Capitalise sentences. Expand voice commands: comma -> ","  period / full stop -> "."  question mark -> "?"  exclamation point -> "!"  new line -> newline  new paragraph -> double newline.
6. Two or more items in succession -> NUMBERED Markdown list. Be aggressive — when in doubt, list it. Triggers include: "one X two Y three Z", "first X second Y third Z", "first is X second is Y", "X then Y then Z", "also X also Y", or any colon-lead-in followed by 2+ items. **Lead-in prose and trailing prose around the list stay as prose; ONLY the enumerated section becomes the list.** Drop the spoken numbers/ordinals/connectors ("one", "first", "first is", "then", "also"). Items with a short title get formatted as "1. Title - description." with a literal hyphen-space.
7. Fully restructure for clarity. Break long run-on thoughts into separate sentences or paragraphs. Group related ideas together even if the speaker scattered them. Pull out lists, headings, and paragraph breaks whenever they make the content easier to read. Use Markdown formatting (numbered lists, bullet lists, bold for emphasis on key terms, paragraph breaks) freely wherever it improves clarity.
8. Preserve every distinct point and every concrete detail. You may reorder, regroup, and rephrase for clarity, but never drop content, never summarise away substance, and never invent new facts.
9. Full restarts: when the speaker abandons a whole thought and starts over ("scratch all that", "forget that", "let me start again"), keep ONLY the restarted version. The abandoned thought and the restart marker disappear completely.
10. Developer dictation: write technical terms in their canonical form. Spoken casing commands apply to the exact words of the identifier they describe, keeping every word: "camel case parse config file" -> parseConfigFile; "fetch user settings camel case" -> fetchUserSettings; "snake case user id" -> user_id; "kebab case my new branch" -> my-new-branch; "all caps max retries" -> MAX_RETRIES. The spoken casing command itself disappears from the output — never echo "camel case" or write it in parentheses. Correct mis-transcribed product and tool names to their real spelling (Vercel, Supabase, GitHub, PostgreSQL, npm, kubectl, ctranslate2).

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

Input:  racism is fun
Output: Racism is fun.

Input:  he said racism is amazing and that made me worried
Output: He said racism is amazing, and that made me worried.

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

Input:  so i was thinking we could refactor the auth module first and then maybe scratch all that let's just ship the hotfix friday
Output: Let's just ship the hotfix Friday.

Input:  rename the function to get user profile camel case and add a user id snake case column to the orders table
Output: Rename the function to getUserProfile and add a user_id column to the orders table.

Input:  we deploy on versel and use superbase for the database
Output: We deploy on Vercel and use Supabase for the database.

Input:  hey sarah new paragraph thanks for sending the report over period i'll review it tonight and send notes tomorrow new paragraph best comma lucy
Output: Hey Sarah,

Thanks for sending the report over. I'll review it tonight and send notes tomorrow.

Best,
Lucy

CRITICAL: Numbered list items ALWAYS go on their own lines, separated by literal newlines. Never put "1. X 2. Y" on one line.

CRITICAL: Spoken commands are instructions to APPLY, never words to keep. "comma" becomes "," — the word "comma" never appears. "camel case" / "snake case" / "all caps" change the casing of the identifier they describe and then vanish — never echo them, never put them in parentheses.

MULTILINGUAL:
Keep the output in the same language as the input text. All rules above apply in every language. Never translate."""


def build_system_prompt(vocab: str = "") -> str:
    """The polish system prompt, plus preferred spellings when vocab given."""
    vocab = vocab.strip()
    if not vocab:
        return POLISH_SYSTEM_PROMPT
    return POLISH_SYSTEM_PROMPT + f"\n\nPreferred spellings (use these exactly): {vocab}"


def _notify(summary: str, body: str = "", urgency: str = "normal") -> None:
    """Best-effort user notification; never raises, never needs notify.py."""
    try:
        from .notify import notify
    except Exception:
        print(f"[{summary}] {body}", file=sys.stderr)
        return
    try:
        notify(summary, body, urgency)
    except Exception:
        print(f"[{summary}] {body}", file=sys.stderr)


def _ollama_error_hint(e: Exception, model: str) -> str:
    """Turn a requests failure into an actionable fix suggestion."""
    s = str(e)
    if isinstance(e, requests.ConnectionError):
        return "ollama not running — try `systemctl status ollama`"
    if isinstance(e, requests.Timeout):
        return "ollama timeout — model may be cold; raising polish.timeout may help"
    if "404" in s:
        return f"model not pulled — `ollama pull {model}`"
    return s


def _openai_error_hint(e: Exception, cfg: PolishConfig) -> str:
    s = str(e)
    if isinstance(e, requests.ConnectionError):
        if (cfg.backend or "").strip().lower() == "lmstudio":
            return (
                f"no server at {cfg.url} — in LM Studio, open the Developer "
                "tab and turn the local server on"
            )
        return f"no server at {cfg.url} — is your OpenAI-compatible server running?"
    if isinstance(e, requests.Timeout):
        return "polish timeout — raising polish.timeout may help"
    if "401" in s or "403" in s:
        return "authorization rejected — check polish.api_key"
    if "404" in s:
        return f"endpoint or model not found — check polish.url ({cfg.url}) and polish.model ({cfg.model})"
    return s


def _gpu_free_mb() -> int | None:
    """Free VRAM on GPU 0 in MiB, or None if nvidia-smi unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            timeout=2,
            text=True,
        )
        if r.returncode == 0:
            return int(r.stdout.strip().splitlines()[0])
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return None


class VramWatchdog:
    """Unloads the Ollama polish model early when VRAM gets contested.

    Ollama already evicts after `keep_alive` (set per request). This layer
    adds: if another process needs more VRAM than is free, dump the polish
    model so it can have it. The model reloads on the next polish call.
    Only meaningful for the ollama backend; `start()` is a no-op when
    cfg.vram_unload_below_mb <= 0 or nvidia-smi is missing.
    """

    def __init__(self, cfg: PolishConfig, *, poll_s: float = 60.0):
        self.cfg = cfg
        self.threshold_mb = cfg.vram_unload_below_mb
        self.poll_s = poll_s
        self._loaded = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.threshold_mb <= 0 or not shutil.which("nvidia-smi"):
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="voicisst-vram-watchdog"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def note_use(self) -> None:
        """Record that the polish model is (or just became) loaded."""
        self._loaded = True

    def check_once(self) -> bool:
        """One watchdog tick. Returns True if the model was unloaded."""
        if not self._loaded:
            return False
        free = _gpu_free_mb()
        if free is None or free >= self.threshold_mb:
            return False
        try:
            requests.post(
                f"{self.cfg.url}/api/generate",
                json={"model": self.cfg.model, "keep_alive": 0},
                timeout=5,
            )
            print(
                f"unloaded {self.cfg.model} (free VRAM {free}MB < "
                f"{self.threshold_mb}MB threshold)",
                file=sys.stderr,
            )
            self._loaded = False
            return True
        except requests.RequestException as e:
            print(f"unload request failed: {e}", file=sys.stderr)
            return False

    def _loop(self) -> None:
        # Tick every poll_s seconds. Skips while the model isn't loaded.
        while not self._stop.wait(self.poll_s):
            self.check_once()


class Polisher(ABC):
    """Reworks raw dictated speech into clear written text."""

    @abstractmethod
    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        """Polished text; on any failure returns `text` unchanged."""

    @abstractmethod
    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        """Yields successive FULL-TEXT snapshots; the last yield is final.
        On any failure yields `text` once."""

    def warm(self) -> None:  # noqa: B027 — deliberate no-op default
        """Preload the model so the first polish is fast. Best-effort."""

    def unload(self) -> None:  # noqa: B027 — deliberate no-op default
        """Best-effort: free the model's VRAM/RAM."""


class OllamaPolisher(Polisher):
    """Polish via Ollama's /api/generate. Ported from the prototype.

    Think-mode hybrid: thinking can help on long inputs but adds many
    seconds of latency, so below cfg.think_min_chars it is always off.

    Thinking is controlled two ways, negotiated on the first request:
    - Ollama-native thinking models (qwen3.5, deepseek-r1, ...) honor the
      `think` API field; without `think: false` they burn the whole
      num_predict budget on a separate `thinking` channel and return an
      EMPTY response.
    - HF-pulled GGUFs have no thinking template marker and reject the
      `think` field with a 400; they get the prototype's /no_think
      prompt-marker fallback, and any inline <think> blocks are stripped.
    """

    def __init__(self, cfg: PolishConfig, watchdog: VramWatchdog | None = None):
        self.cfg = cfg
        self.watchdog = watchdog if watchdog is not None else VramWatchdog(cfg)
        self.watchdog.start()
        # None = unknown until the first request settles it.
        self._think_field_supported: bool | None = None

    # -- payload helpers ----------------------------------------------------

    def _options(self) -> dict[str, object]:
        opts: dict[str, object] = {
            "temperature": 0.1,
            "num_predict": self.cfg.num_predict,
            "num_ctx": self.cfg.num_ctx,
        }
        # -1 means "let ollama decide" — omit the key entirely.
        if self.cfg.num_gpu >= 0:
            opts["num_gpu"] = self.cfg.num_gpu
        return opts

    def _payload(self, text: str, vocab: str, *, stream: bool) -> tuple[dict[str, object], bool]:
        """Base payload (no thinking control yet) + whether to think."""
        use_think = self.cfg.think and len(text) >= self.cfg.think_min_chars
        payload: dict[str, object] = {
            "model": self.cfg.model,
            "system": build_system_prompt(vocab),
            "prompt": text,
            "stream": stream,
            "keep_alive": self.cfg.keep_alive,
            "options": self._options(),
        }
        return payload, use_think

    @staticmethod
    def _with_think_field(payload: dict[str, object], use_think: bool) -> dict[str, object]:
        p = dict(payload)
        p["think"] = use_think
        return p

    @staticmethod
    def _with_think_marker(payload: dict[str, object], use_think: bool) -> dict[str, object]:
        if use_think:
            return dict(payload)
        p = dict(payload)
        p["system"] = f"{p['system']}\n/no_think"
        p["prompt"] = f"{p['prompt']} /no_think"
        return p

    def _post_generate(
        self, payload: dict[str, object], use_think: bool, *, stream: bool, timeout: float
    ) -> requests.Response:
        """POST /api/generate, negotiating `think`-field support once.

        Returns a response with raise_for_status() already applied.
        """
        url = f"{self.cfg.url}/api/generate"
        if self._think_field_supported is not False:
            r = requests.post(
                url,
                json=self._with_think_field(payload, use_think),
                timeout=timeout,
                stream=stream,
            )
            if r.status_code == 400 and "think" in r.text.lower():
                # Model template has no thinking support (HF GGUF) — fall
                # back to the /no_think prompt marker from here on.
                self._think_field_supported = False
                r.close()
            else:
                r.raise_for_status()
                self._think_field_supported = True
                return r
        r = requests.post(
            url,
            json=self._with_think_marker(payload, use_think),
            timeout=timeout,
            stream=stream,
        )
        r.raise_for_status()
        return r

    # -- Polisher API ---------------------------------------------------------

    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        if not text:
            return text
        try:
            payload, use_think = self._payload(text, vocab, stream=False)
            r = self._post_generate(payload, use_think, stream=False, timeout=self.cfg.timeout)
            data = r.json()
            cleaned = (data.get("response") or "").strip()
            # Strip any leaked <think>...</think> block, then wrapper quotes.
            cleaned = strip_quotes(strip_think(cleaned))
            self.watchdog.note_use()
            if not cleaned and data.get("done_reason") == "length":
                _notify(
                    "polish truncated",
                    "the model spent its whole budget thinking — raise "
                    "polish.num_predict or set polish.think = false",
                    urgency="normal",
                )
            return cleaned or text
        except requests.RequestException as e:
            _notify("polish failed", _ollama_error_hint(e, self.cfg.model), urgency="normal")
            return text

    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        if not text:
            yield text
            return
        try:
            payload, use_think = self._payload(text, vocab, stream=True)
            with self._post_generate(
                payload, use_think, stream=True, timeout=self.cfg.timeout
            ) as r:
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
                        visible = strip_think(accumulated)
                        if visible and visible != last_visible:
                            last_visible = visible
                            yield strip_quotes(visible)
                    if chunk.get("done"):
                        final = strip_quotes(strip_think(accumulated)) or text
                        if final != last_visible:
                            yield final
                        self.watchdog.note_use()
                        return
        except requests.RequestException as e:
            _notify("polish failed", _ollama_error_hint(e, self.cfg.model), urgency="normal")
            yield text

    def warm(self) -> None:
        """Prime the polish path: same system prompt and options as the real
        polish() call so Ollama's KV cache for the system prompt is hot
        before the first user utterance."""
        try:
            payload, use_think = self._payload("hello", "", stream=False)
            self._post_generate(payload, use_think, stream=False, timeout=120)
        except requests.RequestException:
            pass

    def unload(self) -> None:
        try:
            requests.post(
                f"{self.cfg.url}/api/generate",
                json={"model": self.cfg.model, "keep_alive": 0},
                timeout=5,
            )
        except requests.RequestException:
            pass


class OpenAICompatPolisher(Polisher):
    """Polish via any OpenAI-compatible /v1/chat/completions endpoint
    (llama.cpp server, vLLM, LM Studio, OpenAI itself, ...)."""

    def __init__(self, cfg: PolishConfig):
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        last = text
        for snapshot in self.polish_stream(text, language=language, vocab=vocab):
            last = snapshot
        return last

    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        if not text:
            yield text
            return
        url = self.cfg.url.rstrip("/") + "/v1/chat/completions"
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": build_system_prompt(vocab)},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
            "max_tokens": self.cfg.num_predict,
            "stream": True,
        }
        try:
            with requests.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.cfg.timeout,
                stream=True,
            ) as r:
                r.raise_for_status()
                accumulated = ""
                last_visible = ""
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = (
                        raw_line.decode("utf-8", "replace")
                        if isinstance(raw_line, bytes)
                        else raw_line
                    )
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if not delta:
                        continue
                    accumulated += delta
                    visible = strip_think(accumulated)
                    if visible and visible != last_visible:
                        last_visible = visible
                        yield strip_quotes(visible)
                final = strip_quotes(strip_think(accumulated)) or text
                if final != last_visible:
                    yield final
        except requests.RequestException as e:
            _notify("polish failed", _openai_error_hint(e, self.cfg), urgency="normal")
            yield text


def get_polisher(cfg: Config | PolishConfig) -> Polisher | None:
    """Build the configured polisher, or None when polish is off.

    Accepts either the full Config or just its PolishConfig section.
    """
    pc: PolishConfig = getattr(cfg, "polish", cfg)  # type: ignore[assignment]
    if not pc.enabled:
        return None
    backend = (pc.backend or "").strip().lower()
    if backend in ("", "none"):
        return None
    if backend == "ollama":
        return OllamaPolisher(pc)
    if backend in ("lmstudio", "lm-studio", "lm_studio"):
        # LM Studio speaks the OpenAI API on its own port. If the URL is
        # still the Ollama default, point it at LM Studio's instead.
        if pc.url.rstrip("/") == OLLAMA_DEFAULT_URL:
            pc = dataclasses.replace(pc, url=LMSTUDIO_DEFAULT_URL)
        return OpenAICompatPolisher(pc)
    if backend in ("openai", "openai-compat", "openai_compat"):
        return OpenAICompatPolisher(pc)
    _notify(
        "unknown polish backend",
        f"polish.backend = {pc.backend!r} — use 'ollama', 'lmstudio', "
        "'openai' or 'none'; polish disabled",
        urgency="normal",
    )
    return None
