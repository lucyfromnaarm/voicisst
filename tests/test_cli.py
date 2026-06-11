"""CLI (click) wiring + selftest — all heavy paths monkeypatched."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from click.testing import CliRunner

import flow_dictation.audio as audio_mod
import flow_dictation.cli as cli_mod
import flow_dictation.clipboard as clipboard_mod
import flow_dictation.config as config_mod
import flow_dictation.inject as inject_mod
import flow_dictation.selftest as selftest_mod
import flow_dictation.server as server_mod
from flow_dictation import __version__
from flow_dictation.engine.base import EngineError
from flow_dictation.hotkeys.evdev_listener import EvdevListener
from flow_dictation.hotkeys.pynput_listener import PynputListener
from helpers import FakeEngine, make_audio


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config_path() (used by init/path/show and load_config) at tmp."""
    p = tmp_path / "conf" / "config.toml"
    monkeypatch.setattr(config_mod, "config_path", lambda: p)
    return p


class FakeApp:
    """Stand-in for DictationApp: records its wiring, run() returns."""

    instances: list[FakeApp] = []

    def __init__(self, cfg, engine, injector=None, **kwargs):
        self.cfg = cfg
        self.engine = engine
        self.injector = injector
        self.ran = False
        FakeApp.instances.append(self)

    def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        pass


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch) -> type[FakeApp]:
    FakeApp.instances = []
    monkeypatch.setattr(cli_mod, "DictationApp", FakeApp)
    return FakeApp


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
            "--config", str(tmp_path / "missing.toml"),
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
        cli_mod.cli, ["run", "--stream", "--config", str(tmp_path / "missing.toml")]
    )
    assert res.exit_code == 0, res.output
    assert fake_app.instances[0].cfg.output.stream is True


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
            "--config", str(tmp_path / "missing.toml"),
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
        sys, "argv", ["flow", "run", "--config", str(tmp_path / "missing.toml")]
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


@pytest.fixture
def selftest_env(monkeypatch: pytest.MonkeyPatch):
    """Fake out every hardware probe the selftest touches."""
    monkeypatch.setattr(EvdevListener, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(PynputListener, "available", classmethod(lambda cls: True))
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
