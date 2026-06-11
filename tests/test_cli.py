"""CLI (click) wiring + selftest — all heavy paths monkeypatched."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from click.testing import CliRunner

import voicisst.audio as audio_mod
import voicisst.cli as cli_mod
import voicisst.clipboard as clipboard_mod
import voicisst.config as config_mod
import voicisst.inject as inject_mod
import voicisst.selftest as selftest_mod
import voicisst.server as server_mod
import voicisst.tray as tray_mod
from helpers import FakeEngine, make_audio
from voicisst import __version__
from voicisst.engine.base import EngineError
from voicisst.hotkeys.evdev_listener import EvdevListener
from voicisst.hotkeys.pynput_listener import PynputListener


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config_path() (used by init/path/show and load_config) at tmp."""
    p = tmp_path / "conf" / "config.toml"
    monkeypatch.setattr(config_mod, "config_path", lambda: p)
    return p


def write_config(tmp_path: Path, text: str = "") -> Path:
    """An existing (possibly empty) config file — --config requires one now."""
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


class FakeApp:
    """Stand-in for DictationApp: records its wiring, run() returns."""

    instances: list[FakeApp] = []

    def __init__(self, cfg, engine, injector=None, **kwargs):
        self.cfg = cfg
        self.engine = engine
        self.injector = injector
        self.ran = False
        self.stopped = False
        FakeApp.instances.append(self)

    def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch) -> type[FakeApp]:
    FakeApp.instances = []
    monkeypatch.setattr(cli_mod, "DictationApp", FakeApp)
    return FakeApp


class FakeThread:
    """threading.Thread stand-in: runs its target synchronously on start()."""

    instances: list[FakeThread] = []

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self.target = target
        self.args = tuple(args)
        self.kwargs = dict(kwargs or {})
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = False
        FakeThread.instances.append(self)

    def start(self) -> None:
        self.started = True
        if self.target is not None:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None) -> None:
        self.joined = True


@pytest.fixture
def fake_threads(monkeypatch: pytest.MonkeyPatch) -> type[FakeThread]:
    """Replace cli.py's view of the threading module (and only cli.py's)."""
    FakeThread.instances = []
    monkeypatch.setattr(cli_mod, "threading", SimpleNamespace(Thread=FakeThread))
    return FakeThread


# ---------------------------------------------------------------------------
# version / config


def test_version(runner):
    res = runner.invoke(cli_mod.cli, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.output


def test_config_path_prints_location(runner, tmp_config_path):
    res = runner.invoke(cli_mod.cli, ["config", "path"])
    assert res.exit_code == 0
    assert str(tmp_config_path) in res.output


def test_config_init_show_roundtrip(runner, tmp_config_path):
    res = runner.invoke(cli_mod.cli, ["config", "init"])
    assert res.exit_code == 0, res.output
    assert str(tmp_config_path) in res.output
    assert tmp_config_path.read_text(encoding="utf-8") == config_mod.default_config_toml()

    # Never overwrite silently.
    res = runner.invoke(cli_mod.cli, ["config", "init"])
    assert res.exit_code != 0
    assert "already exists" in res.output

    res = runner.invoke(cli_mod.cli, ["config", "init", "--force"])
    assert res.exit_code == 0

    # show: effective config, TOML-ish, includes every section.
    res = runner.invoke(cli_mod.cli, ["config", "show"])
    assert res.exit_code == 0, res.output
    for section in ("[engine]", "[whisper]", "[polish]", "[hotkey]", "[output]"):
        assert section in res.output
    assert "model = " in res.output


# ---------------------------------------------------------------------------
# --config validation: a typo'd path must be a hard error, never silently
# ignored (load_config falls back to defaults for missing files).


def test_config_option_rejects_missing_file(runner, tmp_path):
    res = runner.invoke(cli_mod.cli, ["run", "--config", str(tmp_path / "nope.toml")])
    assert res.exit_code == 2
    assert "does not exist" in res.output
    assert "nope.toml" in res.output


def test_config_option_rejects_directory(runner, tmp_path):
    res = runner.invoke(cli_mod.cli, ["serve", "--config", str(tmp_path)])
    assert res.exit_code == 2
    assert "is a directory" in res.output


def test_main_missing_config_exits_2(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["voicisst", "run", "--config", str(tmp_path / "nope.toml")])
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# run wiring


def test_run_flags_override_config(runner, fake_app, monkeypatch, tmp_path):
    engine = FakeEngine()
    captured = {}

    def fake_get_engine(cfg):
        captured["cfg"] = cfg
        return engine

    monkeypatch.setattr(cli_mod, "get_engine", fake_get_engine)
    res = runner.invoke(
        cli_mod.cli,
        [
            "run",
            "--server", "http://big-box:8765",
            "--token", "sekret",
            "--toggle",
            "--no-stream",
            "--language", "en",
            "--config", str(write_config(tmp_path)),
        ],
    )
    assert res.exit_code == 0, res.output
    app = fake_app.instances[0]
    cfg = app.cfg
    assert cfg is captured["cfg"]
    assert cfg.engine.mode == "remote"
    assert cfg.engine.server_url == "http://big-box:8765"
    assert cfg.engine.token == "sekret"
    assert cfg.hotkey.mode == "toggle"
    assert cfg.output.stream is False
    assert cfg.whisper.language == "en"
    assert app.engine is engine
    assert app.ran


def test_run_stream_flag(runner, fake_app, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "get_engine", lambda cfg: FakeEngine())
    res = runner.invoke(
        cli_mod.cli, ["run", "--stream", "--config", str(write_config(tmp_path))]
    )
    assert res.exit_code == 0, res.output
    assert fake_app.instances[0].cfg.output.stream is True


# ---------------------------------------------------------------------------
# --tray threading: pystray needs the MAIN thread on macOS (AppKit), so the
# run command inverts its arrangement there — app in a thread, tray on main.


def test_tray_darwin_runs_tray_on_main_thread(
    runner, fake_app, fake_threads, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli_mod, "get_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(sys, "platform", "darwin")
    tray_calls: list[tuple] = []

    def fake_run_tray(app, cfg):
        tray_calls.append((app, cfg))

    monkeypatch.setattr(tray_mod, "run_tray", fake_run_tray)
    res = runner.invoke(cli_mod.cli, ["run", "--tray", "--config", str(write_config(tmp_path))])
    assert res.exit_code == 0, res.output
    app = fake_app.instances[0]
    # The only spawned thread runs the app; the tray ran inline (main thread).
    assert [t.target for t in fake_threads.instances] == [app.run]
    assert tray_calls and tray_calls[0][0] is app
    assert app.ran
    assert app.stopped  # tray exit stops the app
    assert fake_threads.instances[0].joined


def test_tray_darwin_ctrl_c_stops_app_and_exits_cleanly(
    runner, fake_app, fake_threads, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli_mod, "get_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(sys, "platform", "darwin")

    def interrupted_tray(app, cfg):
        raise KeyboardInterrupt

    monkeypatch.setattr(tray_mod, "run_tray", interrupted_tray)
    res = runner.invoke(cli_mod.cli, ["run", "--tray", "--config", str(write_config(tmp_path))])
    assert res.exit_code == 0, res.output
    app = fake_app.instances[0]
    assert app.stopped
    assert fake_threads.instances[0].joined


def test_tray_other_platforms_keep_app_on_main_thread(
    runner, fake_app, fake_threads, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli_mod, "get_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(sys, "platform", "linux")
    tray_calls: list[tuple] = []

    def fake_run_tray(app, cfg):
        tray_calls.append((app, cfg))

    monkeypatch.setattr(tray_mod, "run_tray", fake_run_tray)
    res = runner.invoke(cli_mod.cli, ["run", "--tray", "--config", str(write_config(tmp_path))])
    assert res.exit_code == 0, res.output
    app = fake_app.instances[0]
    # The only spawned thread runs the tray; the app ran inline (main thread).
    assert [t.target for t in fake_threads.instances] == [fake_run_tray]
    assert fake_threads.instances[0].args == (app, app.cfg)
    assert app.ran
    assert tray_calls and tray_calls[0][0] is app


def test_bare_invocation_defaults_to_run(runner, fake_app, monkeypatch, tmp_config_path):
    monkeypatch.setattr(cli_mod, "get_engine", lambda cfg: FakeEngine())
    res = runner.invoke(cli_mod.cli, [])
    assert res.exit_code == 0, res.output
    assert fake_app.instances and fake_app.instances[0].ran


# ---------------------------------------------------------------------------
# serve wiring


def test_serve_overrides_and_calls_server(runner, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(server_mod, "serve", lambda cfg: captured.setdefault("cfg", cfg))
    res = runner.invoke(
        cli_mod.cli,
        [
            "serve",
            "--host", "0.0.0.0",
            "--port", "9001",
            "--token", "tok",
            "--config", str(write_config(tmp_path)),
        ],
    )
    assert res.exit_code == 0, res.output
    cfg = captured["cfg"]
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9001
    assert cfg.server.token == "tok"


# ---------------------------------------------------------------------------
# main(): friendly EngineError handling, no traceback


def test_main_engine_error_prints_hint_and_exits_1(monkeypatch, capsys, tmp_path):
    def boom(cfg):
        raise EngineError("engine went boom", hint="try turning it off and on")

    monkeypatch.setattr(cli_mod, "get_engine", boom)
    monkeypatch.setattr(
        sys, "argv", ["voicisst", "run", "--config", str(write_config(tmp_path))]
    )
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "engine went boom" in err
    assert "try turning it off and on" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# selftest


def _fake_recorder_class(samples: np.ndarray):
    class FakeRecorder:
        def __init__(self, samplerate=16000, device=None):
            self.samplerate = samplerate
            self.chunks: list[np.ndarray] = []

        def start(self) -> None:
            pass

        def stop(self):
            return samples, 1000.0

        def is_active(self) -> bool:
            return False

        def elapsed_ms(self) -> float:
            return 0.0

    return FakeRecorder


# Keycodes for the fake evdev module (subset of real evdev.ecodes.ecodes).
_EVDEV_CODES = {
    "KEY_COMPOSE": 127,
    "KEY_MENU": 139,
    "KEY_RIGHTALT": 100,
    "KEY_F9": 67,
    "KEY_A": 30,
}


def fake_evdev_module(caps: list[int], *, name: str = "Fake Keyboard") -> types.ModuleType:
    """An importable evdev stand-in with one device exposing keycodes `caps`."""
    mod = types.ModuleType("evdev")
    ecodes = SimpleNamespace(EV_KEY=1, KEY_BACKSPACE=14, ecodes=dict(_EVDEV_CODES))

    class InputDevice:
        def __init__(self, path: str):
            self.path = path
            self.fd = 99
            self.name = name

        def capabilities(self):
            return {ecodes.EV_KEY: list(caps)}

        def close(self):
            pass

    mod.ecodes = ecodes
    mod.InputDevice = InputDevice
    mod.list_devices = lambda: ["/dev/input/event0"]
    return mod


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def fake_requests_module(get) -> types.ModuleType:
    """A requests stand-in whose get() is the supplied callable."""
    mod = types.ModuleType("requests")
    mod.get = get
    return mod


@pytest.fixture
def selftest_env(monkeypatch: pytest.MonkeyPatch):
    """Fake out every hardware/network probe the selftest touches.

    The fake evdev exposes the default Linux hotkeys; the fake requests
    serves an ollama /api/tags listing that contains the default model.
    """
    monkeypatch.setattr(EvdevListener, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(PynputListener, "available", classmethod(lambda cls: True))
    monkeypatch.setitem(
        sys.modules,
        "evdev",
        fake_evdev_module([_EVDEV_CODES["KEY_COMPOSE"], _EVDEV_CODES["KEY_MENU"]]),
    )
    default_model = config_mod.PolishConfig().model
    monkeypatch.setitem(
        sys.modules,
        "requests",
        fake_requests_module(
            lambda url, timeout=None: FakeResponse({"models": [{"name": default_model}]})
        ),
    )
    monkeypatch.setattr(audio_mod, "Recorder", _fake_recorder_class(make_audio(0.1)))
    monkeypatch.setattr(inject_mod, "get_injector", lambda cfg: SimpleNamespace(name="fake"))
    monkeypatch.setattr(clipboard_mod, "copy", lambda text: True)
    monkeypatch.setattr(selftest_mod, "_read_clipboard", lambda: None)
    return monkeypatch


def test_selftest_passes_with_fake_engine(selftest_env, tmp_path, capsys):
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    engine = FakeEngine()
    rc = selftest_mod.run_selftest(cfg, engine=engine, audio_seconds=0.01)
    assert rc == 0
    assert engine.warmed == 1  # local mode warms (lazy model load)
    err = capsys.readouterr().err
    assert "PASS" in err
    assert "FAIL" not in err


def test_selftest_fails_on_silent_audio(selftest_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        audio_mod, "Recorder", _fake_recorder_class(np.zeros(0, dtype=np.float32))
    )
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    assert "FAIL" in capsys.readouterr().err


def test_selftest_skips_polish_when_disabled(selftest_env, tmp_path, capsys):
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.polish.enabled = False
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 0
    assert "SKIP" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# selftest: polish backend probe (engines swallow polish failures at runtime,
# so the round-trip alone can never fail — the probe must).


def test_selftest_polish_fails_when_ollama_down(selftest_env, monkeypatch, tmp_path, capsys):
    def dead_get(url, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setitem(sys.modules, "requests", fake_requests_module(dead_get))
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ollama not running" in err
    assert "systemctl status ollama" in err


def test_selftest_polish_fails_when_model_missing(selftest_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(
        sys.modules,
        "requests",
        fake_requests_module(
            lambda url, timeout=None: FakeResponse({"models": [{"name": "other:1b"}]})
        ),
    )
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    err = capsys.readouterr().err
    assert "model not pulled" in err
    assert f"ollama pull {cfg.polish.model}" in err


def test_selftest_polish_fails_when_text_comes_back_unchanged(selftest_env, tmp_path, capsys):
    class EchoPolishEngine(FakeEngine):
        def polish(self, text, *, language=None, vocab=""):
            return text  # exactly what a swallowed backend failure looks like

    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    rc = selftest_mod.run_selftest(cfg, engine=EchoPolishEngine(), audio_seconds=0.01)
    assert rc == 1
    assert "unchanged" in capsys.readouterr().err


def test_selftest_polish_passes_when_model_present_and_text_changes(
    selftest_env, tmp_path, capsys
):
    # The selftest_env fixture serves tags containing the configured model,
    # and FakeEngine.polish prefixes its input — both probe and round-trip OK.
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 0
    assert "sample:" in capsys.readouterr().err


def test_selftest_polish_openai_backend_unreachable(selftest_env, monkeypatch, tmp_path, capsys):
    def dead_get(url, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setitem(sys.modules, "requests", fake_requests_module(dead_get))
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.polish.backend = "openai"
    cfg.polish.url = "http://localhost:9999/v1"
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    err = capsys.readouterr().err
    assert "unreachable" in err
    assert "polish.url" in err


def test_selftest_polish_remote_mode_skips_local_probe(selftest_env, monkeypatch, tmp_path, capsys):
    def must_not_be_called(url, timeout=None):
        raise AssertionError("local polish probe must not run in remote mode")

    monkeypatch.setitem(sys.modules, "requests", fake_requests_module(must_not_be_called))
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.engine.mode = "remote"
    cfg.engine.server_url = "http://big-box:8765"
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 0, capsys.readouterr().err


# ---------------------------------------------------------------------------
# selftest: hotkey device check (a readable /dev/input is not enough — some
# keyboard must actually expose the configured keycodes).


def test_selftest_hotkey_fails_when_no_device_exposes_key(
    selftest_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setitem(sys.modules, "evdev", fake_evdev_module([_EVDEV_CODES["KEY_A"]]))
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.hotkey.backend = "evdev"
    cfg.hotkey.keys = ["KEY_COMPOSE", "KEY_MENU"]
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no keyboard exposes KEY_COMPOSE, KEY_MENU" in err
    assert "KEY_RIGHTALT" in err
    assert "python -m evdev.evtest" in err


def test_selftest_hotkey_passes_when_device_exposes_key(
    selftest_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setitem(sys.modules, "evdev", fake_evdev_module([_EVDEV_CODES["KEY_F9"]]))
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.hotkey.backend = "evdev"
    cfg.hotkey.keys = ["KEY_F9"]
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 0
    assert "expose [KEY_F9]" in capsys.readouterr().err


def test_selftest_hotkey_permission_error_mentions_input_group(
    selftest_env, monkeypatch, tmp_path, capsys
):
    # Pin to linux: hotkey defaults and the evdev availability gate read
    # sys.platform, so on mac/win the input-group path is never reached.
    monkeypatch.setattr(sys, "platform", "linux")
    mod = fake_evdev_module([_EVDEV_CODES["KEY_COMPOSE"]])

    def denied():
        raise PermissionError("/dev/input/event0: permission denied")

    mod.list_devices = denied
    monkeypatch.setitem(sys.modules, "evdev", mod)
    cfg = config_mod.load_config(path=tmp_path / "missing.toml", env={})
    cfg.hotkey.backend = "evdev"
    rc = selftest_mod.run_selftest(cfg, engine=FakeEngine(), audio_seconds=0.01)
    assert rc == 1
    assert "usermod -aG input" in capsys.readouterr().err
