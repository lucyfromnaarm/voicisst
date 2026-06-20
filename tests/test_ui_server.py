"""ui/server.py: token auth, config round-trips, state WS, audio, capture, warm."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tomlkit
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import FakeEngine
from voicisst.config import Config, default_config_toml
from voicisst.engine.base import EngineError
from voicisst.events import StateBus
from voicisst.ui import server as ui_server
from voicisst.ui.server import create_ui_app, serve_ui

TOKEN = "ui-t0ken-abc123"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def make_client(
    cfg: Config,
    *,
    bus: StateBus | None = None,
    engine: FakeEngine | None = None,
    token: str = TOKEN,
    config_file: Path | None = None,
    authed: bool = True,
) -> TestClient:
    app = create_ui_app(cfg, bus=bus, engine=engine, token=token, config_file=config_file)
    client = TestClient(app)
    if authed and token:
        client.headers["X-Voicisst-Token"] = token
    return client


def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------------------
# Token auth matrix


def test_no_token_403_json_for_api(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    r = client.get("/api/meta")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]
    assert body["hint"]


def test_no_token_403_html_for_pages(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    r = client.get("/")
    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"]
    assert "link" in r.text.lower()


def test_wrong_token_rejected_via_every_channel(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    assert client.get("/api/meta?t=wrong").status_code == 403
    assert client.get("/api/meta", headers={"X-Voicisst-Token": "wrong"}).status_code == 403
    client.cookies.set("voicisst_ui", "wrong")
    assert client.get("/api/meta").status_code == 403
    # ...but a correct query token wins even with a stale cookie present.
    assert client.get(f"/api/meta?t={TOKEN}").status_code == 200


def test_query_token_sets_httponly_strict_cookie(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    r = client.get(f"/api/meta?t={TOKEN}")
    assert r.status_code == 200
    raw = r.headers["set-cookie"].lower()
    assert "voicisst_ui=" in raw
    assert "httponly" in raw
    assert "samesite=strict" in raw
    # The cookie alone now authorizes follow-up requests (TestClient keeps it).
    assert client.get("/api/meta").status_code == 200


def test_header_token_accepted(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    r = client.get("/api/meta", headers={"X-Voicisst-Token": TOKEN})
    assert r.status_code == 200


def test_cookie_token_accepted(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    client.cookies.set("voicisst_ui", TOKEN)
    assert client.get("/api/meta").status_code == 200


def test_empty_token_disables_auth(cfg: Config, config_file: Path) -> None:
    # create_ui_app(token="") is the bare-test convenience; serve_ui always
    # generates a real per-run token.
    client = make_client(cfg, token="", config_file=config_file, authed=False)
    assert client.get("/api/meta").status_code == 200


def test_token_comparison_is_constant_time() -> None:
    import inspect

    src = inspect.getsource(ui_server)
    assert "hmac.compare_digest" in src


# ---------------------------------------------------------------------------
# Pages and static files


def test_index_serves_index_html_and_static_assets(
    cfg: Config, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>UI SENTINEL</html>", encoding="utf-8")
    (static / "app.js").write_text("// js sentinel", encoding="utf-8")
    monkeypatch.setattr(ui_server, "STATIC_DIR", static)
    client = make_client(cfg, config_file=config_file)
    r = client.get("/")
    assert r.status_code == 200
    assert "UI SENTINEL" in r.text
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "js sentinel" in r.text
    # static files honor auth too
    bare = make_client(cfg, config_file=config_file, authed=False)
    assert bare.get("/static/app.js").status_code == 403
    assert bare.get("/").status_code == 403


def test_index_placeholder_when_frontend_missing(
    cfg: Config, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "empty-static"
    static.mkdir()
    monkeypatch.setattr(ui_server, "STATIC_DIR", static)
    client = make_client(cfg, config_file=config_file)
    r = client.get("/")
    assert r.status_code == 200
    assert "voicisst" in r.text.lower()


# ---------------------------------------------------------------------------
# /api/meta


def test_meta(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/meta").json()
    assert body["version"]
    assert body["platform"] == sys.platform
    assert body["onboarded"] is False
    assert body["config_path"] == str(config_file)
    assert body["dictation_running"] is False
    config_file.write_text("[audio]\n", encoding="utf-8")
    assert client.get("/api/meta").json()["onboarded"] is True


def test_meta_dictation_running_with_bus(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, bus=StateBus(), config_file=config_file)
    assert client.get("/api/meta").json()["dictation_running"] is True


# ---------------------------------------------------------------------------
# /api/config GET / PUT


def test_config_get_without_file_returns_template(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/config").json()
    assert body["toml"] == default_config_toml()
    assert body["path"] == str(config_file)
    assert body["values"]["audio"]["min_record_ms"] == 300
    assert body["values"]["replacements"] == {}


def test_config_get_reads_file(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MIN_RECORD_MS", raising=False)
    monkeypatch.delenv("VOICISST_AUDIO_MIN_RECORD_MS", raising=False)
    config_file.write_text("# mine\n[audio]\nmin_record_ms = 500\n", encoding="utf-8")
    body = make_client(cfg, config_file=config_file).get("/api/config").json()
    assert "# mine" in body["toml"]
    assert body["values"]["audio"]["min_record_ms"] == 500


def test_config_put_round_trip_preserves_comments(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    text = "# precious comment\n[audio]\nmin_record_ms = 450 # inline note\n"
    r = client.put("/api/config", json={"toml": text})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    if os.name == "posix":
        assert (config_file.stat().st_mode & 0o777) == 0o600
    body = client.get("/api/config").json()
    assert body["toml"] == text  # byte-for-byte: comments intact
    assert body["values"]["audio"]["min_record_ms"] == 450


def test_config_put_invalid_toml_400(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put("/api/config", json={"toml": "[audio\nbroken ="})
    assert r.status_code == 400
    assert r.json()["error"]
    assert not config_file.exists()  # nothing was written


def test_config_put_wrong_body_400(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    assert client.put("/api/config", json={"nope": True}).status_code == 400


# ---------------------------------------------------------------------------
# /api/config/values PUT


def test_config_values_dotted_keys_preserve_comments(cfg: Config, config_file: Path) -> None:
    config_file.write_text(
        "# precious header\n[audio]\nmin_record_ms = 300 # inline note\n",
        encoding="utf-8",
    )
    client = make_client(cfg, config_file=config_file)
    r = client.put(
        "/api/config/values",
        json={
            "values": {
                "audio.min_record_ms": 250,
                "hotkey.mode": "toggle",
                "dictionary.words": ["Lucy", "Octavia"],
                "replacements": {"vs code": "VS Code"},
            }
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    text = config_file.read_text(encoding="utf-8")
    assert "# precious header" in text
    assert "# inline note" in text
    doc = tomlkit.parse(text)
    assert doc["audio"]["min_record_ms"] == 250
    assert doc["hotkey"]["mode"] == "toggle"
    assert list(doc["dictionary"]["words"]) == ["Lucy", "Octavia"]
    assert dict(doc["replacements"]) == {"vs code": "VS Code"}
    if os.name == "posix":
        assert (config_file.stat().st_mode & 0o777) == 0o600


def test_config_values_starts_from_template_when_no_file(
    cfg: Config, config_file: Path
) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put("/api/config/values", json={"values": {"whisper.model": "small"}})
    assert r.json() == {"ok": True}
    text = config_file.read_text(encoding="utf-8")
    assert "Voicisst configuration" in text  # template comments survived
    assert tomlkit.parse(text)["whisper"]["model"] == "small"


def test_config_values_replacements_replace_and_dotted(
    cfg: Config, config_file: Path
) -> None:
    client = make_client(cfg, config_file=config_file)
    assert (
        client.put(
            "/api/config/values",
            json={"values": {"replacements": {"a": "A", "b": "B"}}},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/config/values", json={"values": {"replacements": {"a": "A2"}}}
        ).status_code
        == 200
    )
    doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    assert dict(doc["replacements"]) == {"a": "A2"}  # 'b' removed, 'a' updated
    assert (
        client.put(
            "/api/config/values", json={"values": {"replacements.vs code": "VS Code"}}
        ).status_code
        == 200
    )
    doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    assert doc["replacements"]["vs code"] == "VS Code"


def test_config_values_unknown_key_400_with_suggestion(
    cfg: Config, config_file: Path
) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put("/api/config/values", json={"values": {"audio.min_record_mss": 1}})
    assert r.status_code == 400
    assert "min_record_ms" in r.json()["error"]  # did-you-mean text
    assert not config_file.exists()


def test_config_values_unknown_section_400_with_suggestion(
    cfg: Config, config_file: Path
) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put("/api/config/values", json={"values": {"audoi.min_record_ms": 1}})
    assert r.status_code == 400
    assert "audio" in r.json()["error"]


def test_config_values_bad_value_400(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put(
        "/api/config/values", json={"values": {"audio.min_record_ms": "not-a-number"}}
    )
    assert r.status_code == 400
    assert "min_record_ms" in r.json()["error"]


def test_config_values_replacements_must_be_mapping(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.put("/api/config/values", json={"values": {"replacements": "nope"}})
    assert r.status_code == 400


def test_config_values_empty_body_400(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    assert client.put("/api/config/values", json={}).status_code == 400
    assert client.put("/api/config/values", json={"values": {}}).status_code == 400


# ---------------------------------------------------------------------------
# /api/state + /ws/state


def test_state_idle_without_bus(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/state").json()
    assert body["state"] == "idle"
    assert body["detail"] == ""
    assert "ts" in body


def test_state_reflects_bus(cfg: Config, config_file: Path) -> None:
    bus = StateBus()
    client = make_client(cfg, bus=bus, config_file=config_file)
    bus.publish("listening", "hotkey down")
    body = client.get("/api/state").json()
    assert body["state"] == "listening"
    assert body["detail"] == "hotkey down"


def test_ws_state_pushes_real_bus_events(cfg: Config, config_file: Path) -> None:
    bus = StateBus()
    client = make_client(cfg, bus=bus, config_file=config_file)
    with client.websocket_connect(f"/ws/state?t={TOKEN}") as ws:
        first = ws.receive_json()
        assert first["state"] == "idle"  # the latest event arrives on connect
        bus.publish("listening", "hotkey down")
        msg = ws.receive_json()
        assert msg["state"] == "listening"
        assert msg["detail"] == "hotkey down"
        assert "ts" in msg
        bus.publish("transcribing")
        assert ws.receive_json()["state"] == "transcribing"
    # the subscriber must be removed once the socket closes
    assert _wait_for(lambda: not bus._subs)


def test_ws_state_requires_token(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/state"):
            pass
    assert ei.value.code == 4403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/state?t=wrong"):
            pass


def test_ws_state_cookie_auth_and_idle_without_bus(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file, authed=False)
    with client.websocket_connect(
        "/ws/state", headers={"cookie": f"voicisst_ui={TOKEN}"}
    ) as ws:
        assert ws.receive_json()["state"] == "idle"


# ---------------------------------------------------------------------------
# /api/audio/*


def test_audio_devices_lists_input_capable_only(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = types.ModuleType("sounddevice")
    mod.query_devices = lambda: [
        {"index": 0, "name": "Mic A", "max_input_channels": 2},
        {"index": 1, "name": "Speakers", "max_input_channels": 0},
        {"index": 2, "name": "Mic B", "max_input_channels": 1},
    ]
    mod.default = types.SimpleNamespace(device=(2, 1))
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/audio/devices").json()
    assert body == {
        "devices": [
            {"index": 0, "name": "Mic A", "default": False},
            {"index": 2, "name": "Mic B", "default": True},
        ]
    }


def test_audio_devices_failure_is_helpful(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = types.ModuleType("sounddevice")

    def boom() -> None:
        raise RuntimeError("PortAudio not initialized")

    mod.query_devices = boom
    mod.default = types.SimpleNamespace(device=(-1, -1))
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/audio/devices").json()
    assert body["devices"] == []
    assert "PortAudio" in body["error"]
    assert body["hint"]


def make_fake_sd_capture(*, fill: float | None, fail_start: bool = False) -> types.ModuleType:
    """Fake sounddevice whose InputStream feeds `fill`-valued audio on start."""
    mod = types.ModuleType("sounddevice")

    class InputStream:
        def __init__(self, **kwargs: Any):
            if fail_start:
                raise RuntimeError("Error opening InputStream: no device")
            self.callback = kwargs["callback"]

        def start(self) -> None:
            if fill is not None:
                data = np.full((1600, 1), fill, dtype=np.float32)
                self.callback(data, 1600, None, None)

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    mod.InputStream = InputStream
    return mod


def test_audio_test_ok(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", make_fake_sd_capture(fill=0.1))
    client = make_client(cfg, config_file=config_file)
    body = client.post("/api/audio/test", json={"seconds": 0.1}).json()
    assert body["ok"] is True
    assert abs(body["rms"] - 0.1) < 0.01
    assert abs(body["peak"] - 0.1) < 0.01
    assert body["samples"] == 1600
    assert body["hint"] == ""


def test_audio_test_quiet_hint(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Between muted_rms (1e-5) and rms_gate (0.005): usable but quiet.
    monkeypatch.setitem(sys.modules, "sounddevice", make_fake_sd_capture(fill=0.002))
    client = make_client(cfg, config_file=config_file)
    body = client.post("/api/audio/test", json={"seconds": 0.1}).json()
    assert body["ok"] is True
    assert "quiet" in body["hint"].lower()


def test_audio_test_muted_hint(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", make_fake_sd_capture(fill=0.0))
    client = make_client(cfg, config_file=config_file)
    body = client.post("/api/audio/test", json={"seconds": 0.1}).json()
    assert body["ok"] is False
    assert "mute" in body["hint"].lower()


def test_audio_test_no_device_hint(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sys.modules, "sounddevice", make_fake_sd_capture(fill=None, fail_start=True)
    )
    client = make_client(cfg, config_file=config_file)
    body = client.post("/api/audio/test", json={"seconds": 0.1}).json()
    assert body["ok"] is False
    assert body["samples"] == 0
    assert "microphone" in body["hint"].lower()


# ---------------------------------------------------------------------------
# /api/hotkey/capture (helper mocked per backend)


def test_hotkey_capture_endpoint(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_server, "capture_hotkey", lambda c, t: ("KEY_F9", "evdev"))
    client = make_client(cfg, config_file=config_file)
    r = client.post("/api/hotkey/capture", json={"timeout_s": 3})
    assert r.status_code == 200
    assert r.json() == {"key": "KEY_F9", "backend": "evdev"}


def test_hotkey_capture_timeout_408(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_timeout(c: Config, t: float) -> tuple[str, str]:
        raise TimeoutError(f"no key pressed within {t:g} seconds")

    monkeypatch.setattr(ui_server, "capture_hotkey", raise_timeout)
    client = make_client(cfg, config_file=config_file)
    r = client.post("/api/hotkey/capture", json={"timeout_s": 1})
    assert r.status_code == 408
    body = r.json()
    assert body["error"]
    assert body["hint"]


def test_hotkey_capture_backend_error_503(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_engine_error(c: Config, t: float) -> tuple[str, str]:
        raise EngineError("no readable keyboard", hint="join the input group")

    monkeypatch.setattr(ui_server, "capture_hotkey", raise_engine_error)
    client = make_client(cfg, config_file=config_file)
    r = client.post("/api/hotkey/capture")
    assert r.status_code == 503
    assert r.json() == {"error": "no readable keyboard", "hint": "join the input group"}


def test_hotkey_capture_timeout_capped_at_15s(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_server, "capture_hotkey", lambda c, t: (f"{t}", "fake"))
    client = make_client(cfg, config_file=config_file)
    assert client.post("/api/hotkey/capture", json={"timeout_s": 99}).json()["key"] == "15.0"


def test_capture_hotkey_picks_explicit_backend(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_server, "_capture_evdev", lambda t: "KEY_F5")
    monkeypatch.setattr(ui_server, "_capture_pynput", lambda t: "f5")
    cfg.hotkey.backend = "evdev"
    assert ui_server.capture_hotkey(cfg, 1.0) == ("KEY_F5", "evdev")
    cfg.hotkey.backend = "pynput"
    assert ui_server.capture_hotkey(cfg, 1.0) == ("f5", "pynput")
    cfg.hotkey.backend = "bogus"
    with pytest.raises(EngineError):
        ui_server.capture_hotkey(cfg, 1.0)


def test_capture_hotkey_auto_backend(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_server, "_capture_evdev", lambda t: "KEY_F5")
    monkeypatch.setattr(ui_server, "_capture_pynput", lambda t: "f5")
    cfg.hotkey.backend = "auto"
    key, backend = ui_server.capture_hotkey(cfg, 1.0)
    if sys.platform == "linux":
        assert (key, backend) in {("KEY_F5", "evdev"), ("f5", "pynput")}
    else:
        assert (key, backend) == ("f5", "pynput")


# ---------------------------------------------------------------------------
# evdev capture helper with a fake evdev module


class FakeEvdevDevice:
    def __init__(self, fd: int, name: str = "Fake Keyboard", events: list | None = None):
        self.fd = fd
        self.name = name
        self._events = list(events or [])
        self.closed = False

    def capabilities(self) -> dict:
        return {1: [59, 60]}  # EV_KEY capability

    def read(self):
        if not self._events:
            raise BlockingIOError
        batch, self._events = self._events, []
        return iter(batch)

    def close(self) -> None:
        self.closed = True


def _key_event(code: int, value: int, etype: int = 1) -> types.SimpleNamespace:
    return types.SimpleNamespace(type=etype, code=code, value=value)


def install_fake_evdev(monkeypatch: pytest.MonkeyPatch, devices: list) -> None:
    mod = types.ModuleType("evdev")
    eco = types.ModuleType("evdev.ecodes")
    eco.EV_KEY = 1
    eco.KEY = {59: "KEY_F1", 60: ["KEY_F2", "KEY_F2_ALIAS"]}
    mod.ecodes = eco
    paths = [f"/dev/input/event{i}" for i in range(len(devices))]
    mapping = dict(zip(paths, devices, strict=True))
    mod.list_devices = lambda: list(paths)
    mod.InputDevice = lambda path: mapping[path]
    monkeypatch.setitem(sys.modules, "evdev", mod)
    monkeypatch.setitem(sys.modules, "evdev.ecodes", eco)


def test_capture_evdev_returns_first_key_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # socketpair, not os.pipe: Windows select() only accepts sockets.
    rsock, wsock = socket.socketpair()
    try:
        dev = FakeEvdevDevice(
            rsock.fileno(),
            events=[
                _key_event(0, 1, etype=0),  # not EV_KEY: ignored
                _key_event(59, 0),  # key up: ignored
                _key_event(60, 1),  # key down -> list-valued name
            ],
        )
        install_fake_evdev(monkeypatch, [dev])
        wsock.send(b"x")  # make the fd readable for select()
        assert ui_server._capture_evdev(1.0) == "KEY_F2"
        assert dev.closed  # devices ALWAYS released
    finally:
        wsock.close()
        rsock.close()


def test_capture_evdev_times_out_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    rsock, wsock = socket.socketpair()
    try:
        dev = FakeEvdevDevice(rsock.fileno())
        install_fake_evdev(monkeypatch, [dev])
        with pytest.raises(TimeoutError):
            ui_server._capture_evdev(0.15)
        assert dev.closed
    finally:
        wsock.close()
        rsock.close()


def test_capture_evdev_skips_ydotool_and_errors_helpfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rsock, wsock = socket.socketpair()
    try:
        dev = FakeEvdevDevice(rsock.fileno(), name="ydotoold virtual device")
        install_fake_evdev(monkeypatch, [dev])
        with pytest.raises(EngineError) as ei:
            ui_server._capture_evdev(0.2)
        assert "input" in ei.value.hint
        assert dev.closed
    finally:
        wsock.close()
        rsock.close()


# ---------------------------------------------------------------------------
# pynput capture helper with a fake pynput module


def install_fake_pynput(monkeypatch: pytest.MonkeyPatch, key: Any) -> list:
    mod = types.ModuleType("pynput")
    kb = types.ModuleType("pynput.keyboard")
    created: list[Any] = []

    class Listener:
        def __init__(self, on_press: Any = None, suppress: bool = False, **kwargs: Any):
            self.on_press = on_press
            self.suppress = suppress
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            if key is not None:
                self.on_press(key)

        def stop(self) -> None:
            self.stopped = True

    kb.Listener = Listener
    mod.keyboard = kb
    monkeypatch.setitem(sys.modules, "pynput", mod)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb)
    return created


def test_capture_pynput_special_key(monkeypatch: pytest.MonkeyPatch) -> None:
    created = install_fake_pynput(
        monkeypatch, types.SimpleNamespace(char=None, name="alt_r")
    )
    assert ui_server._capture_pynput(1.0) == "alt_r"
    assert created[0].stopped  # listener ALWAYS released
    assert created[0].suppress is False  # never steal the key


def test_capture_pynput_char_key(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pynput(monkeypatch, types.SimpleNamespace(char="A", name=None))
    assert ui_server._capture_pynput(1.0) == "a"


def test_capture_pynput_times_out_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    created = install_fake_pynput(monkeypatch, None)
    with pytest.raises(TimeoutError):
        ui_server._capture_pynput(0.1)
    assert created[0].stopped


# ---------------------------------------------------------------------------
# Engine: warm state machine, health, lazy build


class SlowWarmEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def warm(self) -> None:
        self.gate.wait(2.0)
        super().warm()


class FailingWarmEngine(FakeEngine):
    def warm(self) -> None:
        raise EngineError("model exploded", hint="try a smaller model")


def test_engine_warm_state_machine(cfg: Config, config_file: Path) -> None:
    engine = SlowWarmEngine()
    client = make_client(cfg, engine=engine, config_file=config_file)
    assert client.get("/api/engine/warm").json() == {"status": "idle", "detail": ""}
    assert client.post("/api/engine/warm").json() == {"status": "loading"}
    assert client.get("/api/engine/warm").json()["status"] == "loading"
    # a second POST while loading must not start a second warm thread
    assert client.post("/api/engine/warm").json() == {"status": "loading"}
    engine.gate.set()
    assert _wait_for(lambda: client.get("/api/engine/warm").json()["status"] == "ready")
    assert client.get("/api/engine/warm").json() == {"status": "ready", "detail": ""}
    assert engine.warmed == 1


def test_engine_warm_error_reported(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FailingWarmEngine(), config_file=config_file)
    assert client.post("/api/engine/warm").json() == {"status": "loading"}
    assert _wait_for(lambda: client.get("/api/engine/warm").json()["status"] == "error")
    detail = client.get("/api/engine/warm").json()["detail"]
    assert "model exploded" in detail
    assert "smaller model" in detail  # the hint is surfaced too


def test_engine_health(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FakeEngine(), config_file=config_file)
    body = client.get("/api/engine/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "fake"


def test_engine_endpoints_503_when_engine_unbuildable(
    cfg: Config, config_file: Path
) -> None:
    cfg.engine.mode = "remote"  # no server_url -> get_engine raises EngineError
    client = make_client(cfg, config_file=config_file)  # engine=None: lazy build
    r = client.get("/api/engine/health")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]
    assert body["hint"]
    assert client.post("/api/polish/test", json={"text": "hi"}).status_code == 503
    assert client.post("/api/engine/warm").status_code == 503


def test_engine_lazily_built_once(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import voicisst.engine as engine_pkg

    built: list[Config] = []

    def fake_get_engine(c: Config) -> FakeEngine:
        built.append(c)
        return FakeEngine()

    monkeypatch.setattr(engine_pkg, "get_engine", fake_get_engine)
    client = make_client(cfg, config_file=config_file)  # engine=None
    assert client.get("/api/engine/health").status_code == 200
    assert client.post("/api/polish/test").status_code == 200
    assert len(built) == 1  # built lazily, exactly once


# ---------------------------------------------------------------------------
# /api/files/jobs


def test_file_job_upload_processes_audio_file(cfg: Config, config_file: Path) -> None:
    from helpers import make_wav

    cfg.audio.normalize = False
    cfg.dictionary.words = ["Lucy"]
    engine = FakeEngine()
    client = make_client(cfg, engine=engine, config_file=config_file)
    r = client.post(
        "/api/files/jobs?polish=true&language=en&chunk_seconds=120",
        content=make_wav(0.5, 16000),
        headers={
            "content-type": "application/octet-stream",
            "x-voicisst-filename": "voice.wav",
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    box: dict[str, Any] = {}

    def done() -> bool:
        box["body"] = client.get(f"/api/files/jobs/{job_id}").json()
        return box["body"].get("status") in ("done", "error")

    assert _wait_for(done)
    body = box["body"]
    assert body["status"] == "done", body
    raw = "raw:8000@16000:en:Lucy"
    assert body["result"]["raw"] == raw
    assert body["result"]["text"] == f"polished:{raw}"
    assert body["result"]["chunks"] == 1


def test_file_job_upload_requires_body(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FakeEngine(), config_file=config_file)
    r = client.post("/api/files/jobs", content=b"")
    assert r.status_code == 400
    assert "empty" in r.json()["error"]


def test_file_job_unknown_id_404(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FakeEngine(), config_file=config_file)
    r = client.get("/api/files/jobs/missing")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/polish/test


def test_polish_test_custom_text(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FakeEngine(), config_file=config_file)
    body = client.post("/api/polish/test", json={"text": "hello"}).json()
    assert body == {"result": "polished:hello", "changed": True}


def test_polish_test_default_sample(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, engine=FakeEngine(), config_file=config_file)
    body = client.post("/api/polish/test").json()  # no body at all
    assert body["result"].startswith("polished:")
    assert "um" in body["result"]  # the default sample contains fillers
    assert body["changed"] is True


# ---------------------------------------------------------------------------
# /api/polish/models


class _FakeModelsResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_polish_models_ollama(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _FakeModelsResponse:
        calls.append(url)
        return _FakeModelsResponse({"models": [{"name": "qwen3.5:4b"}, {"name": "a-model"}]})

    monkeypatch.setattr("requests.get", fake_get)
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/polish/models").json()
    assert body == {"models": ["a-model", "qwen3.5:4b"]}  # sorted, names only
    assert calls == ["http://localhost:11434/api/tags"]


def test_polish_models_lmstudio_uses_openai_api_and_port(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _FakeModelsResponse:
        calls.append(url)
        return _FakeModelsResponse({"data": [{"id": "qwen2.5-7b-instruct"}]})

    monkeypatch.setattr("requests.get", fake_get)
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/polish/models?backend=lmstudio").json()
    assert body == {"models": ["qwen2.5-7b-instruct"]}
    # cfg.polish.url is still the Ollama default, so LM Studio's port is used
    assert calls == ["http://localhost:1234/v1/models"]


def test_polish_models_respects_query_url(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _FakeModelsResponse:
        calls.append(url)
        return _FakeModelsResponse({"models": []})

    monkeypatch.setattr("requests.get", fake_get)
    client = make_client(cfg, config_file=config_file)
    r = client.get("/api/polish/models?backend=ollama&url=http://big-box:11434/")
    assert r.json() == {"models": []}
    assert calls == ["http://big-box:11434/api/tags"]


def test_polish_models_never_sends_api_key_to_request_supplied_host(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg.polish.api_key = "sk-secret"
    seen_headers: list[dict] = []

    def fake_get(url: str, **kw: Any) -> _FakeModelsResponse:
        seen_headers.append(kw.get("headers") or {})
        return _FakeModelsResponse({"data": []})

    monkeypatch.setattr("requests.get", fake_get)
    client = make_client(cfg, config_file=config_file)
    # Host from the query string, not the config: the key must stay home.
    client.get("/api/polish/models?backend=openai&url=http://attacker.example")
    assert seen_headers == [{}]

    # Same host as the configured polish.url: the key goes along as normal.
    cfg.polish.url = "http://localhost:11434"
    client.get("/api/polish/models?backend=openai&url=http://localhost:11434")
    assert seen_headers[1].get("Authorization") == "Bearer sk-secret"


def test_polish_models_backend_down_gives_hint(
    cfg: Config, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    def fake_get(url: str, **kw: Any) -> _FakeModelsResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("requests.get", fake_get)
    client = make_client(cfg, config_file=config_file)
    body = client.get("/api/polish/models").json()
    assert body["models"] == []
    assert "ollama" in body["hint"].lower()


def test_polish_models_rejects_non_http_url(cfg: Config, config_file: Path) -> None:
    client = make_client(cfg, config_file=config_file)
    r = client.get("/api/polish/models?url=file:///etc/passwd")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Lazy-import hints + serve_ui


def test_missing_fastapi_gives_install_hint(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(EngineError) as ei:
        create_ui_app(cfg)
    assert "voicisst[ui]" in ei.value.hint


def test_missing_uvicorn_gives_install_hint(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(EngineError) as ei:
        serve_ui(cfg)
    assert "voicisst[ui]" in ei.value.hint


def install_fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    record: dict[str, Any] = {}
    mod = types.ModuleType("uvicorn")

    def run(app: Any, **kwargs: Any) -> None:
        record["app"] = app
        record["kwargs"] = kwargs

    mod.run = run
    monkeypatch.setitem(sys.modules, "uvicorn", mod)
    return record


def test_serve_ui_prints_url_opens_browser_and_binds_loopback(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    record = install_fake_uvicorn(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    cfg.ui.open_browser = True
    serve_ui(cfg, port=18990)
    assert record["kwargs"]["host"] == "127.0.0.1"  # loopback ONLY
    assert record["kwargs"]["port"] == 18990
    assert record["kwargs"]["log_level"] == "warning"
    assert len(opened) == 1
    url = opened[0]
    assert url.startswith("http://127.0.0.1:18990/?t=")
    token = url.split("?t=", 1)[1]
    assert len(token) >= 16  # token_urlsafe(16) is ~22 chars
    assert url in capsys.readouterr().err  # loud stderr print
    # the app uvicorn got really is guarded by that same per-run token
    client = TestClient(record["app"])
    assert client.get("/api/meta").status_code == 403
    assert client.get(f"/api/meta?t={token}").status_code == 200


def test_serve_ui_unique_token_per_run(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_uvicorn(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    cfg.ui.open_browser = True
    serve_ui(cfg)
    serve_ui(cfg)
    assert opened[0] != opened[1]


def test_serve_ui_honors_open_browser_config_and_override(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = install_fake_uvicorn(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    cfg.ui.open_browser = False
    cfg.ui.web_port = 18991
    serve_ui(cfg)  # cfg says no browser; default port from cfg
    assert opened == []
    assert record["kwargs"]["port"] == 18991
    serve_ui(cfg, open_browser=True)  # explicit override beats cfg
    assert len(opened) == 1
    cfg.ui.open_browser = True
    serve_ui(cfg, open_browser=False)
    assert len(opened) == 1
