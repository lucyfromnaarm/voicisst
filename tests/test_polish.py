"""Tests for voicisst.polish — headless, requests fully mocked."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from voicisst import polish as polish_mod
from voicisst.config import Config, PolishConfig
from voicisst.polish import (
    POLISH_SYSTEM_PROMPT,
    OllamaPolisher,
    OpenAICompatPolisher,
    Polisher,
    VramWatchdog,
    build_system_prompt,
    get_polisher,
)

# ---------------------------------------------------------------------------
# Helpers


def make_cfg(**kw: Any) -> PolishConfig:
    return PolishConfig(**kw)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def close(self) -> None:
        pass

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: oops for url: x")

    def json(self) -> dict:
        return self._payload


class FakeStreamResponse:
    """Context-manager response whose iter_lines() replays canned lines."""

    def __init__(self, lines: list[bytes], status: int = 200):
        self.lines = lines
        self.status_code = status
        self.text = ""

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        pass

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: oops for url: x")

    def iter_lines(self):
        yield from self.lines


def capture_post(monkeypatch: pytest.MonkeyPatch, response: object) -> list[dict]:
    """Replace requests.post with a recorder returning `response`."""
    calls: list[dict] = []

    def fake_post(url: str, **kwargs: Any) -> object:
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(polish_mod.requests, "post", fake_post)
    return calls


def capture_notify(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    notes: list[tuple] = []
    monkeypatch.setattr(polish_mod, "_notify", lambda *a, **k: notes.append(a))
    return notes


def ollama_lines(*tokens: str, done: bool = True) -> list[bytes]:
    lines = [json.dumps({"response": t}).encode() for t in tokens]
    if done:
        lines.append(json.dumps({"done": True}).encode())
    return lines


def sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode()


def sse_delta(content: str) -> bytes:
    return sse({"choices": [{"delta": {"content": content}}]})


LONG_TEXT = "this dictation is comfortably longer than one hundred characters " * 3


# ---------------------------------------------------------------------------
# System prompt


def test_prompt_contains_core_rules() -> None:
    assert "You rework raw dictated speech" in POLISH_SYSTEM_PROMPT
    assert "Output ONLY the reworked text." in POLISH_SYSTEM_PROMPT
    assert "Kill all filler" in POLISH_SYSTEM_PROMPT
    assert "keep ONLY the final version" in POLISH_SYSTEM_PROMPT
    assert "Collapse stutters and repeats" in POLISH_SYSTEM_PROMPT
    assert "NUMBERED Markdown list" in POLISH_SYSTEM_PROMPT
    assert "never drop content" in POLISH_SYSTEM_PROMPT


def test_prompt_contains_examples_verbatim() -> None:
    assert "We should ship the auth refactor this sprint." in POLISH_SYSTEM_PROMPT
    assert "Send the email to Alice." in POLISH_SYSTEM_PROMPT
    assert "Crazy product ideas:" in POLISH_SYSTEM_PROMPT
    assert "1. Self-watering plant shoes - sneakers with built-in planters." in POLISH_SYSTEM_PROMPT
    assert "Meet at 4 pm." in POLISH_SYSTEM_PROMPT
    assert "I need to pick up eggs, milk, and bread on the way home." in POLISH_SYSTEM_PROMPT
    assert "Ideas for the weekend:" in POLISH_SYSTEM_PROMPT


def test_prompt_critical_line() -> None:
    assert (
        "CRITICAL: Numbered list items ALWAYS go on their own lines, separated by "
        'literal newlines. Never put "1. X 2. Y" on one line.' in POLISH_SYSTEM_PROMPT
    )


def test_prompt_developer_dictation_rules() -> None:
    assert "parseConfigFile" in POLISH_SYSTEM_PROMPT  # casing commands
    assert "fetchUserSettings" in POLISH_SYSTEM_PROMPT
    assert "user_id" in POLISH_SYSTEM_PROMPT
    assert "MAX_RETRIES" in POLISH_SYSTEM_PROMPT
    assert "Vercel" in POLISH_SYSTEM_PROMPT  # canonical tool spelling
    assert "Supabase" in POLISH_SYSTEM_PROMPT


def test_prompt_full_restart_rule_and_examples() -> None:
    assert "scratch all that" in POLISH_SYSTEM_PROMPT
    assert "Let's just ship the hotfix Friday." in POLISH_SYSTEM_PROMPT
    assert "getUserProfile" in POLISH_SYSTEM_PROMPT
    # email formatting example
    assert "Thanks for sending the report over." in POLISH_SYSTEM_PROMPT


def test_prompt_multilingual_section_after_critical() -> None:
    assert "Always respond in the same language as the input text." in POLISH_SYSTEM_PROMPT
    assert "All rules above apply in every language." in POLISH_SYSTEM_PROMPT
    assert "Never translate." in POLISH_SYSTEM_PROMPT
    assert POLISH_SYSTEM_PROMPT.index("MULTILINGUAL") > POLISH_SYSTEM_PROMPT.index("CRITICAL")


def test_build_system_prompt_without_vocab() -> None:
    assert build_system_prompt() == POLISH_SYSTEM_PROMPT
    assert build_system_prompt("   ") == POLISH_SYSTEM_PROMPT


def test_build_system_prompt_with_vocab() -> None:
    p = build_system_prompt("Lucy, Naarm, ctranslate2")
    assert p.startswith(POLISH_SYSTEM_PROMPT)
    assert p.endswith("Preferred spellings (use these exactly): Lucy, Naarm, ctranslate2")


# ---------------------------------------------------------------------------
# OllamaPolisher: blocking polish()


def test_ollama_polish_success_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Polished text."}))
    p = OllamaPolisher(make_cfg())
    assert p.polish(LONG_TEXT) == "Polished text."
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://localhost:11434/api/generate"
    assert call["timeout"] == 60.0
    payload = call["json"]
    assert payload["model"] == "qwen3.5:4b"
    assert payload["stream"] is False
    assert payload["keep_alive"] == "30m"
    opts = payload["options"]
    assert opts["temperature"] == 0.1
    assert opts["num_ctx"] == 8192
    assert opts["num_predict"] == 2048
    assert "num_gpu" not in opts  # default -1 means "let backend decide"


def test_ollama_think_field_false_on_short_input(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg(think=True)).polish("short text")
    payload = calls[0]["json"]
    assert payload["think"] is False  # below think_min_chars
    assert payload["prompt"] == "short text"  # no marker on the field path
    assert "/no_think" not in payload["system"]


def test_ollama_think_field_true_on_long_input(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg(think=True)).polish(LONG_TEXT)
    payload = calls[0]["json"]
    assert payload["think"] is True
    assert payload["prompt"] == LONG_TEXT
    assert "/no_think" not in payload["system"]


def test_ollama_think_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg()).polish(LONG_TEXT)
    assert calls[0]["json"]["think"] is False


def test_ollama_marker_fallback_when_think_field_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF GGUFs reject the `think` field with a 400; the /no_think prompt
    marker takes over, and the negotiation result is cached."""
    responses = [
        FakeResponse({"error": '"m" does not support thinking'}, status=400),
        FakeResponse({"response": "Hi."}),
        FakeResponse({"response": "Again."}),
    ]
    calls: list[dict] = []

    def fake_post(url: str, **kwargs: Any) -> object:
        calls.append({"url": url, **kwargs})
        return responses[len(calls) - 1]

    monkeypatch.setattr(polish_mod.requests, "post", fake_post)
    p = OllamaPolisher(make_cfg(think=False))
    assert p.polish("short text") == "Hi."
    assert len(calls) == 2
    retry = calls[1]["json"]
    assert "think" not in retry
    assert retry["prompt"].endswith(" /no_think")
    assert retry["system"].endswith("/no_think")
    # Negotiation cached: the next polish goes straight to the marker path.
    assert p.polish("more text") == "Again."
    assert len(calls) == 3
    assert "think" not in calls[2]["json"]


def test_ollama_think_field_support_cached_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    p = OllamaPolisher(make_cfg())
    p.polish("one")
    p.polish("two")
    assert len(calls) == 2
    assert all("think" in c["json"] for c in calls)


def test_ollama_empty_response_thinking_exhaustion_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thinking model that burns num_predict returns an empty response
    with done_reason=length — fall back to the input and tell the user."""
    capture_post(monkeypatch, FakeResponse({"response": "", "done_reason": "length"}))
    notes = capture_notify(monkeypatch)
    assert OllamaPolisher(make_cfg()).polish("some text") == "some text"
    assert any("truncated" in n[0] for n in notes)


def test_ollama_num_gpu_forwarded_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg(num_gpu=36)).polish("hello")
    assert calls[0]["json"]["options"]["num_gpu"] == 36


def test_ollama_num_gpu_zero_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg(num_gpu=0)).polish("hello")
    assert calls[0]["json"]["options"]["num_gpu"] == 0


def test_ollama_vocab_lands_in_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "Hi."}))
    OllamaPolisher(make_cfg()).polish("hello", vocab="Lucy, Naarm")
    assert "Preferred spellings (use these exactly): Lucy, Naarm" in calls[0]["json"]["system"]


def test_ollama_strips_think_and_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(
        monkeypatch, FakeResponse({"response": '<think>let me see</think>"Hello world."'})
    )
    assert OllamaPolisher(make_cfg()).polish("raw") == "Hello world."


def test_ollama_empty_response_returns_input(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(monkeypatch, FakeResponse({"response": ""}))
    assert OllamaPolisher(make_cfg()).polish("raw words") == "raw words"


def test_ollama_empty_text_skips_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "x"}))
    assert OllamaPolisher(make_cfg()).polish("") == ""
    assert calls == []


def test_ollama_connection_error_returns_input(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = capture_notify(monkeypatch)

    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    assert OllamaPolisher(make_cfg()).polish("keep my words") == "keep my words"
    assert notes and "ollama not running" in notes[0][1]
    assert "systemctl status ollama" in notes[0][1]


def test_ollama_404_hint_mentions_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = capture_notify(monkeypatch)
    capture_post(monkeypatch, FakeResponse({}, status=404))
    assert OllamaPolisher(make_cfg(model="qwen3.5:4b")).polish("raw") == "raw"
    assert notes and "ollama pull qwen3.5:4b" in notes[0][1]


def test_ollama_timeout_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = capture_notify(monkeypatch)

    def boom(*a: Any, **k: Any) -> object:
        raise requests.Timeout("too slow")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    assert OllamaPolisher(make_cfg()).polish("raw") == "raw"
    assert notes and "timeout" in notes[0][1]


def test_ollama_failure_notifies_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # default _notify (no notify.py needed) must write to stderr only
    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    OllamaPolisher(make_cfg()).polish("raw")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "polish failed" in captured.err


def test_ollama_note_use_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(monkeypatch, FakeResponse({"response": "ok"}))
    p = OllamaPolisher(make_cfg())
    assert p.watchdog._loaded is False
    p.polish("raw")
    assert p.watchdog._loaded is True


# ---------------------------------------------------------------------------
# OllamaPolisher: polish_stream()


def test_ollama_stream_full_text_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(monkeypatch, FakeStreamResponse(ollama_lines("Hello", " world.")))
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Hello", "Hello world."]


def test_ollama_stream_stops_at_done(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ollama_lines("Hi.") + [json.dumps({"response": "IGNORED"}).encode()]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Hi."]
    assert all("IGNORED" not in s for s in snaps)


def test_ollama_stream_strips_think_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(
        monkeypatch,
        FakeStreamResponse(ollama_lines("<think>", "pondering", "</think>", "Done.")),
    )
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Done."]


def test_ollama_stream_unclosed_think_falls_back_on_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_post(monkeypatch, FakeStreamResponse(ollama_lines("<think>", "never closed")))
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw words"))
    assert snaps == ["raw words"]


def test_ollama_stream_final_unquoted_on_done(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(monkeypatch, FakeStreamResponse(ollama_lines('"Hi', ' there"')))
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw"))
    assert snaps[-1] == "Hi there"


def test_ollama_stream_skips_blank_and_bad_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [b"", b"this is not json"] + ollama_lines("Fine.")
    capture_post(monkeypatch, FakeStreamResponse(lines))
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Fine."]


def test_ollama_stream_connection_error_yields_input(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = capture_notify(monkeypatch)

    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    snaps = list(OllamaPolisher(make_cfg()).polish_stream("keep my words"))
    assert snaps == ["keep my words"]
    assert notes and "ollama not running" in notes[0][1]


def test_ollama_stream_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeStreamResponse([]))
    assert list(OllamaPolisher(make_cfg()).polish_stream("")) == [""]
    assert calls == []


def test_ollama_stream_uses_think_field(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeStreamResponse(ollama_lines("ok")))
    list(OllamaPolisher(make_cfg()).polish_stream("tiny"))
    payload = calls[0]["json"]
    assert payload["stream"] is True
    assert payload["prompt"] == "tiny"
    assert payload["think"] is False


# ---------------------------------------------------------------------------
# OllamaPolisher: warm / unload


def test_ollama_warm_primes_same_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "hi"}))
    OllamaPolisher(make_cfg(num_gpu=8)).warm()
    call = calls[0]
    assert call["timeout"] == 120
    payload = call["json"]
    assert payload["prompt"] == "hello"  # think=True default
    assert payload["stream"] is False
    assert payload["options"]["num_gpu"] == 8


def test_ollama_warm_no_think(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({"response": "hi"}))
    OllamaPolisher(make_cfg(think=False)).warm()
    payload = calls[0]["json"]
    assert payload["prompt"] == "hello"
    assert payload["think"] is False


def test_ollama_warm_swallows_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    OllamaPolisher(make_cfg()).warm()  # must not raise


def test_ollama_unload_posts_zero_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({}))
    OllamaPolisher(make_cfg()).unload()
    assert calls[0]["json"] == {"model": "qwen3.5:4b", "keep_alive": 0}


# ---------------------------------------------------------------------------
# OpenAICompatPolisher


def test_openai_stream_sse_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [sse_delta("Hel"), b"", sse_delta("lo."), b"data: [DONE]"]
    calls = capture_post(monkeypatch, FakeStreamResponse(lines))
    cfg = make_cfg(backend="openai", url="http://box:8000")
    snaps = list(OpenAICompatPolisher(cfg).polish_stream("raw", vocab="Lucy"))
    assert snaps == ["Hel", "Hello."]
    call = calls[0]
    assert call["url"] == "http://box:8000/v1/chat/completions"
    payload = call["json"]
    assert payload["stream"] is True
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith(POLISH_SYSTEM_PROMPT)
    assert "Preferred spellings (use these exactly): Lucy" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {"role": "user", "content": "raw"}
    assert "Authorization" not in call["headers"]  # api_key unset


def test_openai_auth_header_when_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeStreamResponse([sse_delta("x"), b"data: [DONE]"]))
    list(OpenAICompatPolisher(make_cfg(api_key="sk-test")).polish_stream("raw"))
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_openai_done_sentinel_stops_consumption(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [sse_delta("Done."), b"data: [DONE]", sse_delta("IGNORED")]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    snaps = list(OpenAICompatPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Done."]


def test_openai_stream_strips_think(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [sse_delta("<think>hmm</think>"), sse_delta("Result."), b"data: [DONE]"]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    snaps = list(OpenAICompatPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["Result."]


def test_openai_stream_without_done_still_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_post(monkeypatch, FakeStreamResponse([sse_delta("All good.")]))
    snaps = list(OpenAICompatPolisher(make_cfg()).polish_stream("raw"))
    assert snaps == ["All good."]


def test_openai_stream_only_think_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [sse_delta("<think>never closed"), b"data: [DONE]"]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    snaps = list(OpenAICompatPolisher(make_cfg()).polish_stream("raw words"))
    assert snaps == ["raw words"]


def test_openai_connection_error_yields_input(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = capture_notify(monkeypatch)

    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    p = OpenAICompatPolisher(make_cfg(backend="openai"))
    assert list(p.polish_stream("keep my words")) == ["keep my words"]
    assert p.polish("keep my words") == "keep my words"
    assert notes and "is your OpenAI-compatible server running?" in notes[0][1]


def test_openai_polish_returns_final_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [sse_delta("Hel"), sse_delta("lo."), b"data: [DONE]"]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    assert OpenAICompatPolisher(make_cfg()).polish("raw") == "Hello."


def test_openai_skips_bad_sse_json(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [b"data: {not json", b": comment", sse_delta("Ok."), b"data: [DONE]"]
    capture_post(monkeypatch, FakeStreamResponse(lines))
    assert list(OpenAICompatPolisher(make_cfg()).polish_stream("raw")) == ["Ok."]


# ---------------------------------------------------------------------------
# get_polisher


def test_get_polisher_none_backend() -> None:
    assert get_polisher(make_cfg(backend="none")) is None


def test_get_polisher_disabled() -> None:
    assert get_polisher(make_cfg(enabled=False, backend="ollama")) is None


def test_get_polisher_ollama() -> None:
    assert isinstance(get_polisher(make_cfg(backend="ollama")), OllamaPolisher)


def test_get_polisher_openai() -> None:
    assert isinstance(get_polisher(make_cfg(backend="openai")), OpenAICompatPolisher)


def test_get_polisher_lmstudio_swaps_default_url() -> None:
    p = get_polisher(make_cfg(backend="lmstudio"))
    assert isinstance(p, OpenAICompatPolisher)
    # the untouched Ollama default URL becomes LM Studio's default
    assert p.cfg.url == "http://localhost:1234"


def test_get_polisher_lmstudio_keeps_custom_url() -> None:
    p = get_polisher(make_cfg(backend="lmstudio", url="http://big-box:9999"))
    assert isinstance(p, OpenAICompatPolisher)
    assert p.cfg.url == "http://big-box:9999"


def test_get_polisher_accepts_full_config() -> None:
    cfg = Config()
    cfg.polish.backend = "ollama"
    p = get_polisher(cfg)
    assert isinstance(p, OllamaPolisher)
    assert isinstance(p, Polisher)
    assert p.cfg is cfg.polish


def test_get_polisher_unknown_backend_warns_and_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = capture_notify(monkeypatch)
    assert get_polisher(make_cfg(backend="banana")) is None
    assert notes and "banana" in notes[0][1]


# ---------------------------------------------------------------------------
# VramWatchdog


def test_watchdog_start_noop_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polish_mod.shutil, "which", lambda _: None)
    w = VramWatchdog(make_cfg(vram_unload_below_mb=1024))
    w.start()
    assert w._thread is None


def test_watchdog_start_noop_without_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polish_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    w = VramWatchdog(make_cfg(vram_unload_below_mb=0))
    w.start()
    assert w._thread is None


def test_watchdog_unloads_when_vram_low(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({}))
    monkeypatch.setattr(polish_mod, "_gpu_free_mb", lambda: 512)
    w = VramWatchdog(make_cfg(vram_unload_below_mb=1024))
    w.note_use()
    assert w.check_once() is True
    assert calls[0]["json"] == {"model": "qwen3.5:4b", "keep_alive": 0}
    assert w._loaded is False


def test_watchdog_skips_when_vram_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_post(monkeypatch, FakeResponse({}))
    monkeypatch.setattr(polish_mod, "_gpu_free_mb", lambda: 4096)
    w = VramWatchdog(make_cfg(vram_unload_below_mb=1024))
    w.note_use()
    assert w.check_once() is False
    assert calls == []
    assert w._loaded is True


def test_watchdog_skips_when_model_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        polish_mod, "_gpu_free_mb", lambda: pytest.fail("must not probe when unloaded")
    )
    w = VramWatchdog(make_cfg(vram_unload_below_mb=1024))
    assert w.check_once() is False


def test_watchdog_unload_failure_keeps_loaded_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> object:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    monkeypatch.setattr(polish_mod, "_gpu_free_mb", lambda: 100)
    w = VramWatchdog(make_cfg(vram_unload_below_mb=1024))
    w.note_use()
    assert w.check_once() is False
    assert w._loaded is True


def test_gpu_free_mb_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polish_mod.shutil, "which", lambda _: None)
    assert polish_mod._gpu_free_mb() is None


def test_gpu_free_mb_parses_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polish_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")

    class FakeRun:
        returncode = 0
        stdout = "8192\n"

    monkeypatch.setattr(polish_mod.subprocess, "run", lambda *a, **k: FakeRun())
    assert polish_mod._gpu_free_mb() == 8192
