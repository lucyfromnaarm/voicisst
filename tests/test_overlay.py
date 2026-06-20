"""Overlay model + wiring, fully headless: Tk is never imported for real.

OverlayModel is plain math with injected timestamps; run_overlay is
exercised with a stubbed tkinter so these pass with no DISPLAY — and,
just as important, without opening a real window on a machine that has
one.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from helpers import FakeEngine
from voicisst import events, overlay
from voicisst.config import Config
from voicisst.dictation import DictationApp
from voicisst.events import StateBus
from voicisst.overlay import (
    DONE,
    HIDDEN,
    LIVE,
    POLISHING,
    TRANSCRIBING,
    OverlayModel,
    device_label,
    run_overlay,
)


def make_model(**kwargs) -> OverlayModel:
    return OverlayModel("Using Test mic", **kwargs)


# -- state transitions ---------------------------------------------------------


def test_starts_hidden():
    m = make_model()
    assert m.mode == HIDDEN
    assert m.tick(0.0) is False
    assert m.bars() == [0.0] * m.bar_count


def test_listening_shows_live_with_caption():
    m = make_model(caption_s=2.5)
    m.on_state(events.LISTENING, now=10.0)
    assert m.mode == LIVE
    assert m.tick(10.0, level=0.05) is True
    assert m.caption_visible(10.0)
    assert m.caption_visible(12.4)
    assert not m.caption_visible(12.6)  # caption expires, pill stays
    assert m.tick(12.6, level=0.05) is True


def test_full_lifecycle_modes():
    m = make_model()
    m.on_state(events.LISTENING, 0.0)
    assert m.mode == LIVE
    m.on_state(events.TRANSCRIBING, 1.0)
    assert m.mode == TRANSCRIBING
    m.on_state(events.POLISHING, 2.0)
    assert m.mode == POLISHING
    m.on_state(events.DELIVERING, 3.0)
    assert m.mode == DONE
    assert m.tick(3.0) is True


def test_idle_right_after_delivering_lets_the_flash_finish():
    m = make_model(done_flash_s=0.4)
    m.on_state(events.DELIVERING, 5.0)
    m.on_state(events.IDLE, 5.01)  # arrives almost immediately
    assert m.mode == DONE
    assert m.tick(5.2) is True  # still flashing
    assert m.tick(5.5) is False  # flash over -> hidden
    assert m.mode == HIDDEN


def test_idle_without_flash_hides():
    m = make_model()
    m.on_state(events.LISTENING, 0.0)
    m.on_state(events.IDLE, 1.0)
    assert m.mode == HIDDEN


@pytest.mark.parametrize("state", [events.ERROR, events.STOPPED, "unknown"])
def test_error_stopped_unknown_hide_immediately(state):
    m = make_model()
    m.on_state(events.LISTENING, 0.0)
    m.on_state(state, 1.0)
    assert m.mode == HIDDEN
    # even mid-flash, an error wins over the green flash
    m.on_state(events.DELIVERING, 2.0)
    m.on_state(state, 2.1)
    assert m.mode == HIDDEN


def test_new_utterance_resets_levels_and_caption():
    m = make_model(caption_s=1.0)
    m.on_state(events.LISTENING, 0.0)
    for _ in range(m.bar_count):
        m.tick(0.1, level=0.18)
    m.on_state(events.IDLE, 2.0)
    m.on_state(events.LISTENING, 10.0)
    assert m.caption_visible(10.5)
    assert all(b == pytest.approx(overlay._BAR_FLOOR) for b in m.bars())


# -- bars ----------------------------------------------------------------------


def test_live_bars_react_to_level():
    m = make_model()
    m.on_state(events.LISTENING, 0.0)
    m.tick(0.0, level=0.0)
    quiet = m.bars()[-1]
    m.tick(0.03, level=0.18)
    loud = m.bars()[-1]
    assert loud == pytest.approx(1.0)
    assert quiet == pytest.approx(overlay._BAR_FLOOR)
    assert loud > quiet


def test_live_bars_scroll_history():
    m = make_model()
    m.on_state(events.LISTENING, 0.0)
    m.tick(0.0, level=0.18)
    for _ in range(3):
        m.tick(0.1, level=0.0)
    bars = m.bars()
    # the loud sample drifted left as silence was appended on the right
    assert bars[-1] == pytest.approx(overlay._BAR_FLOOR)
    assert max(bars) == pytest.approx(1.0)
    assert bars.index(max(bars)) < m.bar_count - 1


@pytest.mark.parametrize("state", [events.TRANSCRIBING, events.POLISHING])
def test_processing_bars_animate_within_bounds(state):
    m = make_model()
    m.on_state(state, 0.0)
    seen = set()
    for _ in range(30):
        m.tick(0.0)
        bars = m.bars()
        assert len(bars) == m.bar_count
        assert all(0.0 < b <= 1.0 for b in bars)
        seen.add(tuple(round(b, 4) for b in bars))
    assert len(seen) > 1  # it moves


def test_done_bars_are_full():
    m = make_model()
    m.on_state(events.DELIVERING, 0.0)
    assert m.bars() == [1.0] * m.bar_count


def test_level_scaling_clamps():
    assert OverlayModel._scale(-1.0) == pytest.approx(overlay._BAR_FLOOR)
    assert OverlayModel._scale(0.0) == pytest.approx(overlay._BAR_FLOOR)
    assert OverlayModel._scale(99.0) == 1.0


def test_colors_distinct_per_mode():
    m = make_model()
    colors = set()
    for state in (events.LISTENING, events.TRANSCRIBING, events.POLISHING, events.DELIVERING):
        m.on_state(state, 0.0)
        colors.add(m.color())
    assert len(colors) == 4


# -- device_label ----------------------------------------------------------------


def test_device_label_explicit_name(cfg: Config):
    cfg.audio.input_device = "Blue Yeti"
    assert device_label(cfg, query_devices=None) == "Using Blue Yeti"


def test_device_label_resolves_default_via_query(cfg: Config):
    cfg.audio.input_device = ""

    def query(device=None, kind=None):
        assert kind == "input"
        return {"name": "Built-in Audio Analog Stereo"}

    assert device_label(cfg, query) == "Using Built-in Audio Analog Stereo"


def test_device_label_resolves_index(cfg: Config):
    cfg.audio.input_device = "3"

    def query(device=None, kind=None):
        assert device == 3
        return {"name": "USB Mic"}

    assert device_label(cfg, query) == "Using USB Mic"


def test_device_label_generic_fallback(cfg: Config):
    cfg.audio.input_device = "pipewire"

    def query(device=None, kind=None):
        raise RuntimeError("no portaudio")

    assert device_label(cfg, query) == "Using the default mic"


def test_device_label_truncates_long_names(cfg: Config):
    cfg.audio.input_device = "X" * 60
    label = device_label(cfg, query_devices=None)
    assert label == "Using " + "X" * 37 + "…"


# -- parse_xrandr_primary -----------------------------------------------------------


def test_xrandr_primary_wins():
    out = (
        "Screen 0: minimum 320 x 200, current 7680 x 2160, maximum 16384 x 16384\n"
        "DP-2 connected primary 3840x2160+3840+0 (normal left) 600mm x 340mm\n"
        "DP-3 connected 3840x2160+0+0 (normal left) 600mm x 340mm\n"
    )
    assert overlay.parse_xrandr_primary(out) == (3840, 0, 3840, 2160)


def test_xrandr_first_connected_when_no_primary():
    out = (
        "HDMI-1 disconnected (normal left inverted)\n"
        "eDP-1 connected 1920x1080+0+0 (normal left) 310mm x 170mm\n"
    )
    assert overlay.parse_xrandr_primary(out) == (0, 0, 1920, 1080)


def test_xrandr_negative_offsets():
    out = "DP-1 connected primary 2560x1440-2560+0 (normal)\n"
    assert overlay.parse_xrandr_primary(out) == (-2560, 0, 2560, 1440)


def test_xrandr_skips_connected_but_inactive():
    out = (
        "HDMI-1 connected (normal left inverted right x axis y axis)\n"
        "eDP-1 connected primary 1920x1080+0+0 (normal left) 310mm x 170mm\n"
    )
    assert overlay.parse_xrandr_primary(out) == (0, 0, 1920, 1080)


@pytest.mark.parametrize("out", ["", "garbage\n", "xrandr: command not found\n"])
def test_xrandr_unparseable_returns_none(out):
    assert overlay.parse_xrandr_primary(out) is None


# -- DictationApp.audio_level ------------------------------------------------------


def test_audio_level_no_recorder(cfg: Config):
    app = DictationApp(cfg, FakeEngine())
    assert app.audio_level() == 0.0


def test_audio_level_reads_last_chunk(cfg: Config):
    app = DictationApp(cfg, FakeEngine())
    app._recorder = types.SimpleNamespace(
        is_active=lambda: True,
        chunks=[np.zeros(160, dtype=np.float32), np.full(160, 0.1, dtype=np.float32)],
    )
    assert app.audio_level() == pytest.approx(0.1)
    app._recorder.chunks.clear()
    assert app.audio_level() == 0.0


def test_audio_level_inactive_recorder(cfg: Config):
    app = DictationApp(cfg, FakeEngine())
    app._recorder = types.SimpleNamespace(is_active=lambda: False, chunks=[])
    assert app.audio_level() == 0.0


# -- run_overlay degradation --------------------------------------------------------


def _stub_tkinter(monkeypatch: pytest.MonkeyPatch):
    """A tkinter whose Tk() always fails, like a machine with no DISPLAY."""

    class TclError(Exception):
        pass

    def busted_tk(*a, **k):
        raise TclError("no display name and no $DISPLAY environment variable")

    tk_mod = types.ModuleType("tkinter")
    tk_mod.TclError = TclError
    tk_mod.Tk = busted_tk
    font_mod = types.ModuleType("tkinter.font")
    tk_mod.font = font_mod
    monkeypatch.setitem(sys.modules, "tkinter", tk_mod)
    monkeypatch.setitem(sys.modules, "tkinter.font", font_mod)


def test_run_overlay_degrades_without_display(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    _stub_tkinter(monkeypatch)
    cfg.audio.input_device = "Test mic"
    bus = StateBus()
    run_overlay(cfg, bus)  # must return, not raise
    assert "running without it" in capsys.readouterr().err
    assert not bus._subs  # unsubscribed on the way out


def test_run_overlay_skips_macos(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.setattr(sys, "platform", "darwin")
    bus = StateBus()
    run_overlay(cfg, bus)
    assert "macOS" in capsys.readouterr().err
    assert not bus._subs


def test_run_overlay_never_publishes(cfg: Config, monkeypatch: pytest.MonkeyPatch):
    """The overlay is a subscriber only: dictation state is never touched."""
    _stub_tkinter(monkeypatch)
    bus = StateBus()
    before = bus.last
    run_overlay(cfg, bus)
    assert bus.last is before
