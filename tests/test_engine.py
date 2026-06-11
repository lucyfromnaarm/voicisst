"""LocalEngine, RemoteEngine and the get_engine factory — all headless."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

from flow_dictation.config import Config, load_config
from flow_dictation.engine import get_engine
from flow_dictation.engine.base import EngineError
from flow_dictation.engine.local import LocalEngine
from flow_dictation.engine.remote import RemoteEngine
from flow_dictation.protocol import decode_wav, from_b64
from helpers import fake_polish_module, fake_transcribe_module

AUDIO_1S = np.zeros(16000, dtype=np.float32)


@pytest.fixture
def local(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, dict[str, Any]]:
    rec: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "flow_dictation.transcribe", fake_transcribe_module(rec))
    monkeypatch.setitem(sys.modules, "flow_dictation.polish", fake_polish_module(rec))
    return cfg, rec


# ---------------------------------------------------------------------------
# get_engine factory


def test_get_engine_local(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    assert isinstance(get_engine(cfg), LocalEngine)


def test_get_engine_remote(cfg: Config) -> None:
    cfg.engine.mode = "remote"
    cfg.engine.server_url = "http://box:8765"
    assert isinstance(get_engine(cfg), RemoteEngine)


def test_get_engine_remote_requires_url(cfg: Config) -> None:
    cfg.engine.mode = "remote"
    with pytest.raises(EngineError) as ei:
        get_engine(cfg)
    assert "--server" in ei.value.hint
    assert "server_url" in ei.value.hint


def test_get_engine_unknown_mode(cfg: Config) -> None:
    cfg.engine.mode = "banana"
    with pytest.raises(EngineError) as ei:
        get_engine(cfg)
    assert "local" in ei.value.hint


# ---------------------------------------------------------------------------
# LocalEngine


def test_local_lazy_init(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    engine = LocalEngine(cfg)
    assert rec.get("transcribers", []) == []  # nothing loaded yet
    assert rec.get("polishers", []) == []
    engine.transcribe(AUDIO_1S, 16000)
    assert len(rec["transcribers"]) == 1
    assert rec["polishers"] == []  # transcribe never touches the polisher
    engine.transcribe(AUDIO_1S, 16000)
    assert len(rec["transcribers"]) == 1  # cached
    engine.polish("hi")
    engine.polish("ho")
    assert len(rec["polishers"]) == 1


def test_local_transcribe_language_precedence(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    cfg.whisper.language = "es"
    engine = LocalEngine(cfg)
    engine.transcribe(AUDIO_1S, 16000)
    assert rec["transcribers"][0].calls[-1]["language"] == "es"  # config fallback
    engine.transcribe(AUDIO_1S, 16000, language="fr")
    assert rec["transcribers"][0].calls[-1]["language"] == "fr"  # explicit wins


def test_local_transcribe_auto_language_is_none(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    assert cfg.whisper.language == "auto"
    engine = LocalEngine(cfg)
    assert engine.transcribe(AUDIO_1S, 16000) == "t:16000:None:"
    assert rec["transcribers"][0].calls[-1]["language"] is None


def test_local_transcribe_passes_vocab_and_empty_audio(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    engine = LocalEngine(cfg)
    assert engine.transcribe(np.zeros(0, dtype=np.float32), 16000) == ""
    assert rec.get("transcribers", []) == []  # short-circuit: no model load
    engine.transcribe(AUDIO_1S, 16000, vocab="Lucy, ME/CFS")
    assert rec["transcribers"][0].calls[-1]["vocab"] == "Lucy, ME/CFS"


def test_local_polish_delegates(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    engine = LocalEngine(cfg)
    assert engine.polish("hello") == "p:hello"
    assert list(engine.polish_stream("hello")) == ["s1:hello", "p:hello"]


def test_local_polish_disabled_returns_input(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    cfg.polish.backend = "none"
    engine = LocalEngine(cfg)
    assert engine.polish("um hello") == "um hello"
    assert list(engine.polish_stream("um hello")) == ["um hello"]
    assert rec["polishers"] == []
    assert engine.health()["polish_backend"] == "none"


def test_local_warm_loads_everything_and_starts_watchdog(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    cfg.polish.vram_unload_below_mb = 512
    engine = LocalEngine(cfg)
    engine.warm()
    assert len(rec["transcribers"]) == 1
    assert len(rec["polishers"]) == 1
    assert rec["polishers"][0].warm_calls == 1
    assert len(rec["watchdogs"]) == 1
    assert rec["watchdogs"][0].started
    assert rec["watchdogs"][0].cfg is cfg.polish
    engine.warm()  # idempotent: no second watchdog
    assert len(rec["watchdogs"]) == 1
    engine.close()
    assert not rec["watchdogs"][0].started
    assert rec["polishers"][0].unload_calls == 1


def test_local_warm_no_watchdog_by_default(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    engine = LocalEngine(cfg)
    engine.warm()
    # The polisher always owns a watchdog object, but with the default
    # vram_unload_below_mb == 0 it must never actually start.
    assert all(not w.started for w in rec["watchdogs"])


def test_local_health(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    engine = LocalEngine(cfg)
    h = engine.health()
    assert h["status"] == "ok"
    assert h["mode"] == "local"
    assert h["whisper_model"] == "auto"  # not loaded yet: configured value
    engine.warm()
    h = engine.health()
    assert h["whisper_model"] == "fake-model"
    assert h["device"] == "cpu"
    assert h["polish_backend"] == "ollama"
    assert h["polish_model"] == cfg.polish.model
    assert "version" in h


def test_local_missing_transcribe_module_hint(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "flow_dictation.transcribe", None)  # forces ImportError
    engine = LocalEngine(cfg)
    with pytest.raises(EngineError) as ei:
        engine.transcribe(AUDIO_1S, 16000)
    assert "flow-dictation[local]" in ei.value.hint


# ---------------------------------------------------------------------------
# LocalEngine stream session


def test_local_stream_partial_dedup_and_growth(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    engine = LocalEngine(cfg)
    session = engine.open_stream(16000)
    assert session is not None
    session.feed(AUDIO_1S)
    assert session.partial() == "t:16000:None:"
    assert session.partial() is None  # unchanged -> None
    session.feed(np.zeros(8000, dtype=np.float32))
    assert session.partial() == "t:24000:None:"


def test_local_stream_partial_needs_half_second(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    session = LocalEngine(cfg).open_stream(16000)
    session.feed(np.zeros(7999, dtype=np.float32))  # < 0.5 s @ 16 kHz
    assert session.partial() is None
    session.feed(np.zeros(1, dtype=np.float32))
    assert session.partial() == "t:8000:None:"


def test_local_stream_partial_busy_lock_returns_none(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    session = LocalEngine(cfg).open_stream(16000)
    session.feed(AUDIO_1S)
    assert session._run_lock.acquire(blocking=False)  # simulate in-flight pass
    try:
        assert session.partial() is None
    finally:
        session._run_lock.release()
    assert session.partial() == "t:16000:None:"


def test_local_stream_finalize_yields_polish_snapshots(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    session = LocalEngine(cfg).open_stream(16000, vocab="OpenVocab")
    session.feed(AUDIO_1S)
    out = list(session.finalize(vocab="FinalVocab"))
    raw = "t:16000:None:FinalVocab"  # finalize vocab wins over open-time vocab
    assert out == [f"s1:{raw}", f"p:{raw}"]
    assert rec["transcribers"][0].calls[-1]["vocab"] == "FinalVocab"
    assert session.partial() is None  # session is spent


def test_local_stream_finalize_falls_back_to_open_vocab(local: tuple[Config, dict]) -> None:
    cfg, rec = local
    session = LocalEngine(cfg).open_stream(16000, vocab="OpenVocab")
    session.feed(AUDIO_1S)
    list(session.finalize())
    assert rec["transcribers"][0].calls[-1]["vocab"] == "OpenVocab"


def test_local_stream_finalize_raw_when_polish_disabled(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    cfg.polish.backend = "none"
    session = LocalEngine(cfg).open_stream(16000)
    session.feed(AUDIO_1S)
    assert list(session.finalize()) == ["t:16000:None:"]


def test_local_stream_finalize_empty_buffer(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    session = LocalEngine(cfg).open_stream(16000)
    assert list(session.finalize()) == [""]


def test_local_stream_cancel_releases_buffer(local: tuple[Config, dict]) -> None:
    cfg, _ = local
    session = LocalEngine(cfg).open_stream(16000)
    session.feed(AUDIO_1S)
    session.cancel()
    assert session.partial() is None
    session.feed(AUDIO_1S)  # ignored after cancel
    assert session._chunks == []
    session.close()  # idempotent


# ---------------------------------------------------------------------------
# RemoteEngine — REST


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class FakeHTTP:
    """Stands in for requests.Session.request; scripted responses in order."""

    def __init__(self, responses: list[Any]):
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, json: Any = None, timeout: Any = None) -> Any:
        self.requests.append({"method": method, "url": url, "json": json, "timeout": timeout})
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    *,
    url: str = "http://box:8765/",
    token: str = "sekrit",
    **overrides: object,
) -> tuple[RemoteEngine, FakeHTTP]:
    cfg = load_config(
        path=tmp_path / "missing.toml",
        env={},
        overrides={
            "engine.mode": "remote",
            "engine.server_url": url,
            "engine.token": token,
            **overrides,
        },
    )
    engine = RemoteEngine(cfg)
    http = FakeHTTP(responses)
    monkeypatch.setattr(engine._session, "request", http)
    return engine, http


def test_remote_transcribe_request_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, http = _remote(
        tmp_path, monkeypatch, [FakeResponse(200, {"text": "hello world"})],
        **{"engine.request_timeout": "7.5"},
    )
    assert engine._session.headers["Authorization"] == "Bearer sekrit"
    audio = np.linspace(-0.5, 0.5, 800, dtype=np.float32)
    out = engine.transcribe(audio, 16000, language="en", vocab="Lucy")
    assert out == "hello world"
    req = http.requests[0]
    assert req["method"] == "POST"
    assert req["url"] == "http://box:8765/v1/transcribe"  # trailing slash stripped
    assert req["timeout"] == 7.5
    body = req["json"]
    assert body["language"] == "en"
    assert body["vocab"] == "Lucy"
    decoded, sr = decode_wav(from_b64(body["audio_b64"]))
    assert sr == 16000
    assert decoded.shape == audio.shape
    assert float(np.max(np.abs(decoded - audio))) <= 1.0 / 32767 + 1e-6


def test_remote_no_token_no_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [], token="")
    assert "Authorization" not in engine._session.headers


def test_remote_language_falls_back_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, http = _remote(
        tmp_path, monkeypatch,
        [FakeResponse(200, {"text": "x"}), FakeResponse(200, {"text": "x"})],
        **{"whisper.language": "es"},
    )
    engine.transcribe(AUDIO_1S, 16000)
    assert http.requests[0]["json"]["language"] == "es"
    engine.transcribe(AUDIO_1S, 16000, language="fr")
    assert http.requests[1]["json"]["language"] == "fr"


def test_remote_polish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, http = _remote(tmp_path, monkeypatch, [FakeResponse(200, {"text": "Clean."})])
    assert engine.polish("um clean", vocab="V") == "Clean."
    req = http.requests[0]
    assert req["url"] == "http://box:8765/v1/polish"
    assert req["json"] == {"text": "um clean", "language": None, "vocab": "V"}


def test_remote_polish_stream_single_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [FakeResponse(200, {"text": "Clean."})])
    assert list(engine.polish_stream("um clean")) == ["Clean."]


def test_remote_polish_stream_yields_input_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [requests.ConnectionError("refused")])
    assert list(engine.polish_stream("um clean")) == ["um clean"]
    assert "flow serve" in capsys.readouterr().err


def test_remote_connection_error_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [requests.ConnectionError("refused")])
    with pytest.raises(EngineError) as ei:
        engine.transcribe(AUDIO_1S, 16000)
    assert "is `flow serve` running at http://box:8765?" in ei.value.hint
    assert "--host 0.0.0.0" in ei.value.hint


def test_remote_timeout_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [requests.Timeout("too slow")])
    with pytest.raises(EngineError) as ei:
        engine.transcribe(AUDIO_1S, 16000)
    assert "request_timeout" in ei.value.hint


def test_remote_401_maps_to_token_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [FakeResponse(401, {"detail": "unauthorized"})])
    with pytest.raises(EngineError) as ei:
        engine.transcribe(AUDIO_1S, 16000)
    assert "401" in str(ei.value)
    assert "--token" in ei.value.hint


def test_remote_500_surfaces_server_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _remote(
        tmp_path, monkeypatch,
        [FakeResponse(500, {"detail": {"message": "model exploded", "hint": "use cpu"}})],
    )
    with pytest.raises(EngineError) as ei:
        engine.transcribe(AUDIO_1S, 16000)
    assert "model exploded" in str(ei.value)
    assert ei.value.hint == "use cpu"


def test_remote_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "ok", "version": "0.1.0", "mode": "local", "whisper_model": "small"}
    engine, http = _remote(tmp_path, monkeypatch, [FakeResponse(200, payload)])
    assert engine.health() == payload
    req = http.requests[0]
    assert req["method"] == "GET"
    assert req["url"] == "http://box:8765/v1/health"
    assert req["timeout"] == 10.0  # capped


def test_remote_url_normalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, http = _remote(
        tmp_path, monkeypatch, [FakeResponse(200, {})], url="box:8765", token=""
    )
    engine.health()
    assert http.requests[0]["url"] == "http://box:8765/v1/health"


def test_remote_requires_url(cfg: Config) -> None:
    cfg.engine.mode = "remote"
    cfg.engine.server_url = "   "
    with pytest.raises(EngineError):
        RemoteEngine(cfg)


# ---------------------------------------------------------------------------
# RemoteEngine — websocket stream (fake `websocket` module)


class FakeWS:
    """Scripted server: replies to finalize with polish/polish/final."""

    def __init__(self) -> None:
        import queue

        self.sent: list[str] = []
        self.binary: list[bytes] = []
        self.incoming: queue.Queue = queue.Queue()
        self.closed = False
        self.respond_to_finalize = True

    def send(self, data: str) -> None:
        self.sent.append(data)
        msg = json.loads(data)
        if msg.get("type") == "finalize" and self.respond_to_finalize:
            self.incoming.put(json.dumps({"type": "polish", "text": "Polishing…"}))
            self.incoming.put(json.dumps({"type": "polish", "text": "Polished."}))
            self.incoming.put(
                json.dumps({"type": "final", "text": "Polished.", "raw": "raw text"})
            )

    def send_binary(self, data: bytes) -> None:
        self.binary.append(data)

    def recv(self) -> str:
        item = self.incoming.get(timeout=5)
        if item is None:
            raise RuntimeError("connection closed")
        return item

    def close(self, *args: object, **kwargs: object) -> None:
        if not self.closed:
            self.closed = True
            self.incoming.put(None)


@pytest.fixture
def fake_websocket(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    created: dict[str, Any] = {}
    mod = types.ModuleType("websocket")

    def create_connection(url: str, timeout: float | None = None) -> FakeWS:
        if created.get("refuse"):
            raise ConnectionRefusedError("connection refused")
        created["url"] = url
        created["timeout"] = timeout
        ws = FakeWS()
        created["ws"] = ws
        return ws

    mod.create_connection = create_connection  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websocket", mod)
    return created


def _wait_for(predicate: Any, timeout: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


def test_remote_ws_session_full_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [])
    session = engine.open_stream(16000, vocab="V")
    assert session is not None
    ws: FakeWS = fake_websocket["ws"]
    assert fake_websocket["url"] == "ws://box:8765/v1/stream?token=sekrit"
    start = json.loads(ws.sent[0])
    assert start == {"type": "start", "sample_rate": 16000, "language": None, "vocab": "V"}

    # binary feed: float32 -> int16 little-endian
    session.feed(np.array([1.0, -1.0, 0.5], dtype=np.float32))
    frame = _wait_for(lambda: ws.binary and ws.binary[0])
    pcm = np.frombuffer(frame, dtype="<i2")
    assert pcm.tolist() == [32767, -32767, 16384]

    # partials: latest unseen wins, then dedup
    ws.incoming.put(json.dumps({"type": "partial", "text": "hello"}))
    assert _wait_for(session.partial) == "hello"
    assert session.partial() is None
    ws.incoming.put(json.dumps({"type": "partial", "text": "hello world"}))
    assert _wait_for(session.partial) == "hello world"

    out = list(session.finalize(vocab="V2"))
    assert out == ["Polishing…", "Polished."]  # final text deduped against last polish
    assert json.loads(ws.sent[-1]) == {"type": "finalize", "vocab": "V2"}
    assert ws.closed


def test_remote_ws_connect_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    fake_websocket["refuse"] = True
    engine, _ = _remote(tmp_path, monkeypatch, [])
    with pytest.raises(EngineError) as ei:
        engine.open_stream(16000)
    assert "flow serve" in ei.value.hint


def test_remote_ws_finalize_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    engine, _ = _remote(
        tmp_path, monkeypatch, [], **{"engine.request_timeout": "0.15"}
    )
    session = engine.open_stream(16000)
    fake_websocket["ws"].respond_to_finalize = False
    with pytest.raises(EngineError, match="did not finish"):
        list(session.finalize())


def test_remote_ws_server_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [])
    session = engine.open_stream(16000)
    ws: FakeWS = fake_websocket["ws"]
    ws.respond_to_finalize = False
    ws.incoming.put(
        json.dumps({"type": "error", "message": "kaput", "hint": "restart the server"})
    )
    with pytest.raises(EngineError, match="kaput") as ei:
        list(session.finalize())
    assert ei.value.hint == "restart the server"


def test_remote_ws_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [])
    session = engine.open_stream(16000)
    ws: FakeWS = fake_websocket["ws"]
    session.cancel()
    assert json.loads(ws.sent[-1]) == {"type": "cancel"}
    assert ws.closed
    session.close()  # idempotent


def test_remote_ws_session_runs_reader_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_websocket: dict[str, Any]
) -> None:
    engine, _ = _remote(tmp_path, monkeypatch, [])
    session = engine.open_stream(16000)
    assert isinstance(session._reader, threading.Thread)
    assert session._reader.daemon
    session.close()
