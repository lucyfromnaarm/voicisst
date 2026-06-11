"""DictationApp end-to-end with fake engine/listener/recorder/injector.

Fully headless: audio.Recorder is monkeypatched, clipboard and window
detection are stubbed, the hotkey listener is injected via the factory.
Each test runs the real app (worker + watchdog threads) in a background
thread and drives it through the fake listener callbacks.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

import flow_dictation.audio as audio_mod
import flow_dictation.dictation as dictation_mod
from flow_dictation.config import Config, OutputConfig, load_config
from flow_dictation.dictation import DictationApp
from flow_dictation.hotkeys.base import HotkeyListener
from flow_dictation.inject.base import Injector
from helpers import FakeEngine, FakeStreamSession, make_audio

LOUD = make_audio(1.5)  # rms ~0.18, well above the gate


def wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


class FakeListener(HotkeyListener):
    def __init__(self, keys, on_press, on_release, on_backspace=None):
        super().__init__(keys, on_press, on_release, on_backspace)
        self.started = False

    @classmethod
    def available(cls) -> bool:
        return True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


class FakeInjector(Injector):
    """Records ops; `screen` reconstructs what the focused window shows."""

    name = "fake"

    def __init__(self):
        super().__init__(OutputConfig())
        self.ops: list[tuple[str, object]] = []

    @classmethod
    def available(cls) -> bool:
        return True

    def type_text(self, text: str) -> bool:
        self.ops.append(("type", text))
        return True

    def backspace(self, n: int) -> bool:
        self.ops.append(("bs", n))
        return True

    def paste_chord(self) -> bool:
        self.ops.append(("paste", None))
        return True

    def tap_escape(self) -> bool:
        self.ops.append(("esc", None))
        return True

    @property
    def screen(self) -> str:
        s = ""
        for op, arg in self.ops:
            if op == "type":
                s += arg  # type: ignore[operator]
            elif op == "bs":
                assert isinstance(arg, int) and arg <= len(s), "over-backspace!"
                s = s[: len(s) - arg]
        return s

    @property
    def pastes(self) -> int:
        return sum(1 for op, _ in self.ops if op == "paste")


def recorder_class(audio_arr: np.ndarray, dur_ms: float, chunks: tuple = ()):
    """A fake audio.Recorder class with scripted stop() output."""

    class FakeRecorder:
        created: list = []

        def __init__(self, samplerate: int = 16000, device=None):
            self.samplerate = samplerate
            self.device = device
            self.chunks: list[np.ndarray] = []
            self._active = False
            FakeRecorder.created.append(self)

        def start(self) -> None:
            self.chunks = [np.asarray(c, dtype=np.float32) for c in chunks]
            self._active = True

        def stop(self):
            self._active = False
            return np.asarray(audio_arr, dtype=np.float32), float(dur_ms)

        def is_active(self) -> bool:
            return self._active

        def elapsed_ms(self) -> float:
            return float(dur_ms) if self._active else 0.0

    return FakeRecorder


@pytest.fixture
def app_cfg(tmp_path: Path) -> Config:
    cfg = load_config(path=tmp_path / "missing.toml", env={})
    cfg.ui.beep = False
    cfg.ui.notify = False
    cfg.audio.min_record_ms = 10
    cfg.dictionary.path = str(tmp_path / "dictionary.txt")
    cfg.dictionary.use_selection = False
    cfg.history.path = str(tmp_path / "history.jsonl")
    return cfg


class Harness:
    """Runs DictationApp in a thread with all hardware faked out."""

    def __init__(
        self,
        cfg: Config,
        engine,
        monkeypatch: pytest.MonkeyPatch,
        *,
        audio_arr: np.ndarray | None = None,
        dur_ms: float = 1500.0,
        chunks: tuple = (),
        window_class: str | None = "code",
    ):
        self.cfg = cfg
        self.engine = engine
        self.injector = FakeInjector()
        self.listener: FakeListener | None = None
        self.copied: list[str] = []
        self.notices: list[tuple[str, str]] = []
        self.error: BaseException | None = None

        self.recorder_cls = recorder_class(
            LOUD if audio_arr is None else audio_arr, dur_ms, chunks
        )
        monkeypatch.setattr(audio_mod, "Recorder", self.recorder_cls)
        monkeypatch.setattr(dictation_mod.clipboard, "copy", self._copy)
        monkeypatch.setattr(
            dictation_mod.clipboard, "read_primary_selection", lambda: ""
        )
        monkeypatch.setattr(
            dictation_mod.windowinfo, "focused_window_class", lambda: window_class
        )
        monkeypatch.setattr(dictation_mod, "notify", self._notify)

        def listener_factory(cfg, on_press, on_release, on_backspace=None):
            self.listener = FakeListener(cfg.hotkey.keys, on_press, on_release, on_backspace)
            return self.listener

        self.app = DictationApp(
            cfg,
            engine,
            injector=self.injector,
            listener_factory=listener_factory,
            watchdog_tick_s=0.02,
        )
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.app.run()
        except BaseException as e:  # surface run() failures to the test
            self.error = e

    def _copy(self, text: str) -> bool:
        self.copied.append(text)
        return True

    def _notify(self, summary: str, body: str = "", urgency: str = "low", *, enabled=True):
        self.notices.append((summary, body))

    def start(self) -> Harness:
        self.thread.start()
        assert wait_until(
            lambda: self.error is not None or (self.listener is not None and self.listener.started)
        ), "app did not come up"
        if self.error is not None:
            raise self.error
        return self

    def finish(self) -> None:
        self.app.stop()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "app did not shut down"
        if self.error is not None:
            raise self.error

    @property
    def recorder(self):
        return self.recorder_cls.created[0] if self.recorder_cls.created else None


# ---------------------------------------------------------------------------
# hold mode: the full press -> record -> release -> transcribe -> polish ->
# deliver pipeline, paste path.


def test_hold_flow_paste(app_cfg, monkeypatch):
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: h.copied)

        raw = f"raw:{LOUD.size}@16000:None:"
        assert h.copied == [f"polished:{raw}"]
        assert h.injector.pastes == 1
        assert ("transcribe", LOUD.size, 16000, None, "") in engine.calls
        assert ("polish", raw, None, "") in engine.calls
        assert wait_until(lambda: engine.warmed >= 1)  # background warm ran
    finally:
        h.finish()
    assert engine.closed >= 1  # clean shutdown closes the engine


def test_toggle_mode_press_toggles_release_ignored(app_cfg, monkeypatch):
    app_cfg.hotkey.mode = "toggle"
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()  # must be ignored in toggle mode
        time.sleep(0.05)
        assert h.recorder.is_active()
        assert not h.copied
        h.listener.on_press()  # second tap stops + delivers
        assert wait_until(lambda: h.copied)
        assert h.copied[0].startswith("polished:raw:")
    finally:
        h.finish()


# ---------------------------------------------------------------------------
# rejection paths


def test_min_record_rejected(app_cfg, monkeypatch):
    app_cfg.audio.min_record_ms = 1000
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch, dur_ms=200.0).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: not h.recorder.is_active())
        time.sleep(0.05)
        assert not any(c[0] == "transcribe" for c in engine.calls)
        assert not h.copied
    finally:
        h.finish()


def test_muted_mic_rejected_with_hint(app_cfg, monkeypatch):
    silent = np.zeros(32000, dtype=np.float32)  # rms 0 < muted_rms
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch, audio_arr=silent, dur_ms=2000.0).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: any(s == "mic muted?" for s, _ in h.notices))
        assert any("pavucontrol" in b for _, b in h.notices)
        assert not any(c[0] == "transcribe" for c in engine.calls)
        assert not h.copied
    finally:
        h.finish()


def test_rms_gate_rejected_silently(app_cfg, monkeypatch):
    # Audible but below the gate: above muted_rms (1e-5), below rms_gate (5e-3).
    quiet = (make_audio(2.0) * 0.005).astype(np.float32)
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch, audio_arr=quiet, dur_ms=2000.0).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: not h.recorder.is_active())
        time.sleep(0.05)
        assert not any(c[0] == "transcribe" for c in engine.calls)
        assert not h.copied
        assert not any(s == "mic muted?" for s, _ in h.notices)
    finally:
        h.finish()


# ---------------------------------------------------------------------------
# delivery details


def test_replacements_applied_to_delivered_text(app_cfg, monkeypatch):
    app_cfg.replacements = {"polished": "SHINY"}
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: h.copied)
        assert h.copied[0] == f"SHINY:raw:{LOUD.size}@16000:None:"
    finally:
        h.finish()


def test_history_written_when_enabled(app_cfg, monkeypatch, tmp_path):
    app_cfg.history.enabled = True
    app_cfg.history.path = str(tmp_path / "deep" / "history.jsonl")  # parent mkdir
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: Path(app_cfg.history.path).is_file())
    finally:
        h.finish()
    lines = Path(app_cfg.history.path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["raw"].startswith("raw:")
    assert entry["text"].startswith("polished:raw:")
    assert entry["app"] == "code"
    assert "T" in entry["ts"]  # ISO timestamp


def test_terminal_copies_instead_of_pasting(app_cfg, monkeypatch):
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch, window_class="kitty").start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: h.copied)
        assert h.injector.pastes == 0  # never paste into a terminal
        assert any(s == "terminal detected" for s, _ in h.notices)
        assert any("Ctrl+Shift+V" in b for _, b in h.notices)
    finally:
        h.finish()


def test_vocab_assembled_from_words_file_and_selection(app_cfg, monkeypatch, tmp_path):
    dict_file = tmp_path / "dictionary.txt"
    dict_file.write_text("Naarm  # the city\n# pure comment\nKubernetes\n\n", encoding="utf-8")
    app_cfg.dictionary.path = str(dict_file)
    app_cfg.dictionary.words = ["Octavia"]
    app_cfg.dictionary.use_selection = True
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch)
    monkeypatch.setattr(
        dictation_mod.clipboard, "read_primary_selection", lambda: "Wispr Flow"
    )
    h.start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.recorder is not None and h.recorder.is_active())
        h.listener.on_release()
        assert wait_until(lambda: h.copied)
        vocab = "Octavia, Naarm, Kubernetes, Wispr Flow"
        assert ("transcribe", LOUD.size, 16000, None, vocab) in engine.calls
        assert ("esc", None) in h.injector.ops  # selection cleared at press time
    finally:
        h.finish()


# ---------------------------------------------------------------------------
# hands-free stops (work without a key release — essential for toggle mode)


def test_max_record_watchdog_stops_recording(app_cfg, monkeypatch):
    app_cfg.audio.max_record_ms = 100  # fake recorder reports 1500ms elapsed
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch).start()
    try:
        h.listener.on_press()  # never released
        assert wait_until(lambda: h.copied)
        assert any(s == "max record hit" for s, _ in h.notices)
    finally:
        h.finish()


def test_silence_auto_stop(app_cfg, monkeypatch):
    app_cfg.audio.auto_stop_silence_s = 0.05
    chunks = (make_audio(0.2), np.zeros(16000, dtype=np.float32))  # speech, then 1s silence
    engine = FakeEngine(supports_stream=False)
    h = Harness(app_cfg, engine, monkeypatch, chunks=chunks).start()
    try:
        h.listener.on_press()  # never released
        assert wait_until(lambda: h.copied)
        assert h.copied[0].startswith("polished:raw:")
    finally:
        h.finish()


# ---------------------------------------------------------------------------
# streaming path


def test_streaming_live_partials_then_polished_replace(app_cfg, monkeypatch):
    app_cfg.output.stream = True
    app_cfg.output.stream_tick_ms = 10
    chunks = (make_audio(0.3), make_audio(0.3))
    engine = FakeEngine(supports_stream=True)
    engine.scripted_partials = ["hello", "hello world"]
    h = Harness(app_cfg, engine, monkeypatch, chunks=chunks).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.injector.screen == "hello world")
        h.listener.on_release()
        n = sum(c.size for c in chunks)
        final = f"polished:raw:{n}@16000:None:"
        assert wait_until(lambda: h.injector.screen == final)
        session = engine.sessions[0]
        assert sum(c.size for c in session.fed) == n  # pump drained every chunk
        assert session.closed
        assert not h.copied  # streaming types in place; no clipboard involved
    finally:
        h.finish()


class GatedEngine(FakeEngine):
    """finalize() blocks between snapshots until the test opens the gate."""

    def __init__(self):
        super().__init__(supports_stream=True)
        self.gate = threading.Event()

    def open_stream(self, sample_rate, *, language=None, vocab=""):
        session = GatedSession(self, sample_rate, language, vocab)
        self.sessions.append(session)
        return session


class GatedSession(FakeStreamSession):
    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        yield "FIRST"
        self.owner.gate.wait(3)
        yield "SECOND"


def test_backspace_cancels_polish_falls_back_to_raw(app_cfg, monkeypatch):
    app_cfg.output.stream = True
    app_cfg.output.stream_tick_ms = 10
    engine = GatedEngine()
    engine.scripted_partials = ["hello raw"]
    h = Harness(app_cfg, engine, monkeypatch, chunks=(make_audio(0.3),)).start()
    try:
        h.listener.on_press()
        assert wait_until(lambda: h.injector.screen == "hello raw")
        # Out-of-window backspace must be a no-op (polish not running yet).
        h.listener.on_backspace()
        h.listener.on_release()
        assert wait_until(lambda: h.injector.screen == "FIRST")
        h.listener.on_backspace()  # inside the polish window -> cancel
        engine.gate.set()
        assert wait_until(lambda: h.injector.screen == "hello raw")
        assert "SECOND" not in h.injector.screen
        assert any(s == "polish cancelled" for s, _ in h.notices)
    finally:
        h.finish()
