"""server/app.py: REST + websocket protocol against a FakeEngine."""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import FakeEngine, fake_polish_module, fake_transcribe_module, make_wav
from voicisst.config import Config
from voicisst.protocol import float_to_pcm16, to_b64
from voicisst.server import create_app, serve

TOKEN = "s3cret"


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def client(engine: FakeEngine) -> TestClient:
    return TestClient(create_app(engine))


@pytest.fixture
def auth_client(engine: FakeEngine) -> TestClient:
    return TestClient(create_app(engine, token=TOKEN))


# ---------------------------------------------------------------------------
# REST


def test_health(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "fake"  # engine fields merged in
    assert body["whisper_model"] == "fake-model"
    assert "version" in body


def test_transcribe_round_trip(client: TestClient) -> None:
    wav = make_wav(0.5, 16000)  # 8000 samples
    r = client.post(
        "/v1/transcribe",
        json={"audio_b64": to_b64(wav), "language": None, "vocab": ""},
    )
    assert r.status_code == 200
    assert r.json() == {"text": "raw:8000@16000:None:"}


def test_transcribe_passes_language_and_vocab(client: TestClient) -> None:
    r = client.post(
        "/v1/transcribe",
        json={"audio_b64": to_b64(make_wav(0.25, 48000)), "language": "en", "vocab": "Lucy"},
    )
    assert r.status_code == 200
    assert r.json() == {"text": "raw:12000@48000:en:Lucy"}


def test_transcribe_bad_payloads(client: TestClient) -> None:
    assert client.post("/v1/transcribe", json={}).status_code == 400
    r = client.post("/v1/transcribe", json={"audio_b64": "!!!notbase64!!!"})
    assert r.status_code == 400
    assert "base64" in r.json()["detail"]
    r = client.post("/v1/transcribe", json={"audio_b64": to_b64(b"not a wav")})
    assert r.status_code == 400
    assert "WAV" in r.json()["detail"]
    r = client.post(
        "/v1/transcribe",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_polish(client: TestClient) -> None:
    r = client.post("/v1/polish", json={"text": "hi", "language": None, "vocab": ""})
    assert r.status_code == 200
    assert r.json() == {"text": "polished:hi"}
    assert client.post("/v1/polish", json={}).status_code == 400


def test_process(client: TestClient, engine: FakeEngine) -> None:
    wav = make_wav(0.5, 16000)
    r = client.post(
        "/v1/process",
        json={"audio_b64": to_b64(wav), "language": "en", "vocab": "V", "polish": True},
    )
    assert r.status_code == 200
    raw = "raw:8000@16000:en:V"
    assert r.json() == {"raw": raw, "text": f"polished:{raw}"}
    assert ("polish", raw, "en", "V") in engine.calls


def test_process_without_polish(client: TestClient) -> None:
    r = client.post(
        "/v1/process", json={"audio_b64": to_b64(make_wav(0.5, 16000)), "polish": False}
    )
    raw = "raw:8000@16000:None:"
    assert r.json() == {"raw": raw, "text": raw}


# ---------------------------------------------------------------------------
# Auth


def test_rest_auth_required(auth_client: TestClient) -> None:
    assert auth_client.get("/v1/health").status_code == 401
    r = auth_client.post("/v1/polish", json={"text": "x"})
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]["hint"]


def test_rest_auth_wrong_token(auth_client: TestClient) -> None:
    r = auth_client.get("/v1/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_rest_auth_correct_token(auth_client: TestClient) -> None:
    r = auth_client.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Websocket streaming


def _pcm_bytes(n_samples: int) -> bytes:
    return float_to_pcm16(np.zeros(n_samples, dtype=np.float32) + 0.1).tobytes()


def test_ws_full_session(client: TestClient) -> None:
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": None, "vocab": ""})
        ws.send_bytes(_pcm_bytes(16000))  # 1 s >= 0.5 s -> partial pass launches
        ws.send_json({"type": "finalize", "vocab": "Names"})
        messages = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "final":
                break
    raw = "raw:16000@16000:None:Names"
    assert messages[0] == {"type": "partial", "text": "raw:16000@16000:None:"}
    assert messages[1] == {"type": "polish", "text": f"polishing:{raw}"}
    assert messages[2] == {"type": "polish", "text": f"polished:{raw}"}
    assert messages[3] == {"type": "final", "text": f"polished:{raw}", "raw": raw}


def test_ws_short_audio_skips_partial(client: TestClient) -> None:
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": "en", "vocab": "V"})
        ws.send_bytes(_pcm_bytes(4000))  # 0.25 s < 0.5 s: no partial pass
        ws.send_json({"type": "finalize"})
        messages = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "final":
                break
    assert [m["type"] for m in messages] == ["polish", "polish", "final"]
    assert messages[-1]["raw"] == "raw:4000@16000:en:V"  # start vocab is the fallback


def test_ws_finalize_with_no_audio(client: TestClient) -> None:
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": None, "vocab": ""})
        ws.send_json({"type": "finalize"})
        msg = ws.receive_json()
    assert msg == {"type": "final", "text": "", "raw": ""}


def test_ws_cancel_closes(client: TestClient, engine: FakeEngine) -> None:
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": None, "vocab": ""})
        ws.send_bytes(_pcm_bytes(100))
        ws.send_json({"type": "cancel"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    # cancel must not run the finalize pipeline
    assert not any(c[0] == "polish_stream" for c in engine.calls)


def test_ws_auth_rejected_before_accept(auth_client: TestClient) -> None:
    # A bad token must deny the upgrade outright (real uvicorn turns the
    # pre-accept close into an HTTP 403 handshake rejection) — the client
    # must not be able to dictate a whole utterance first.
    with pytest.raises(WebSocketDisconnect) as ei:
        with auth_client.websocket_connect("/v1/stream?token=wrong"):
            pass
    assert ei.value.code == 4401


def test_ws_auth_missing_token_rejected(auth_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as ei:
        with auth_client.websocket_connect("/v1/stream"):
            pass
    assert ei.value.code == 4401


def test_ws_auth_query_token_accepted(auth_client: TestClient) -> None:
    with auth_client.websocket_connect(f"/v1/stream?token={TOKEN}") as ws:
        ws.send_json({"type": "finalize"})
        assert ws.receive_json()["type"] == "final"


def test_ws_auth_header_accepted(auth_client: TestClient) -> None:
    with auth_client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as ws:
        ws.send_json({"type": "finalize"})
        assert ws.receive_json()["type"] == "final"


def test_ws_auth_header_preferred_over_query(auth_client: TestClient) -> None:
    # When both are sent, the Authorization header wins (clients should
    # never put the token in the URL — it leaks into access logs).
    with auth_client.websocket_connect(
        "/v1/stream?token=wrong", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as ws:
        ws.send_json({"type": "finalize"})
        assert ws.receive_json()["type"] == "final"


def test_token_comparison_is_constant_time() -> None:
    # Behavioral timing tests are flaky; assert the timing-safe primitive is
    # actually what the auth checks use.
    import inspect

    from voicisst.server import app as app_module

    src = inspect.getsource(app_module)
    assert "hmac.compare_digest" in src
    assert "!= f\"Bearer" not in src  # no plain comparison left behind


# ---------------------------------------------------------------------------
# Size caps (memory-exhaustion DoS protection)


def test_rest_413_over_audio_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voicisst.server.app.MAX_AUDIO_BYTES", 1000)
    wav = make_wav(0.5, 16000)  # 16 kB WAV, over the (patched) 1000-byte cap
    for path in ("/v1/transcribe", "/v1/process"):
        r = client.post(path, json={"audio_b64": to_b64(wav)})
        assert r.status_code == 413
        detail = r.json()["detail"]
        assert "cap" in detail["message"]
        assert "shorter" in detail["hint"]


def test_rest_413_content_length_checked_early(
    client: TestClient, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voicisst.server.app.MAX_AUDIO_BYTES", 1000)
    # Body over cap*4/3 + 64 KiB: rejected from the Content-Length header
    # alone, before the body is parsed or any engine call happens.
    r = client.post("/v1/polish", json={"text": "x" * 70000})
    assert r.status_code == 413
    assert "cap" in r.json()["detail"]["message"]
    assert engine.calls == []


def test_rest_audio_under_cap_accepted(client: TestClient) -> None:
    # The real cap (~32 MiB) must not get in the way of normal utterances.
    r = client.post("/v1/transcribe", json={"audio_b64": to_b64(make_wav(2.0, 16000))})
    assert r.status_code == 200


def test_ws_audio_cap_sends_error_and_closes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voicisst.server.app.MAX_AUDIO_SAMPLES", 8000)
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": None, "vocab": ""})
        ws.send_bytes(_pcm_bytes(8001))  # one sample over the cap
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "cap" in msg["message"]
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 1009  # message too big


def test_ws_audio_under_cap_streams_fine(client: TestClient) -> None:
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "sample_rate": 16000, "language": None, "vocab": ""})
        ws.send_bytes(_pcm_bytes(16000))  # 1 s, far under the ~32 MiB cap
        ws.send_json({"type": "finalize"})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "final":
                break
    assert msg["raw"] == "raw:16000@16000:None:"


# ---------------------------------------------------------------------------
# serve() entry point


def _patch_serve_deps(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list]:
    rec: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "voicisst.transcribe", fake_transcribe_module(rec))
    monkeypatch.setitem(sys.modules, "voicisst.polish", fake_polish_module(rec))
    runs: list = []
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kw: runs.append((app, kw))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    return rec, runs


def test_serve_warns_on_public_host_without_token(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rec, runs = _patch_serve_deps(monkeypatch)
    cfg.server.host = "0.0.0.0"
    serve(cfg)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "--token" in err
    assert len(rec["transcribers"]) == 1  # warmed before serving
    assert rec["polishers"][0].warm_calls == 1
    app, kwargs = runs[0]
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8765
    assert kwargs["log_level"] == "info"


def test_serve_quiet_on_loopback(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _, runs = _patch_serve_deps(monkeypatch)
    serve(cfg)  # default host 127.0.0.1, no token
    assert "WARNING" not in capsys.readouterr().err
    assert runs[0][1]["host"] == "127.0.0.1"


def test_serve_with_token_no_warning(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _, runs = _patch_serve_deps(monkeypatch)
    cfg.server.host = "0.0.0.0"
    cfg.server.token = TOKEN
    serve(cfg)
    assert "WARNING" not in capsys.readouterr().err
