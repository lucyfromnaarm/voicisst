"""On-screen dictation overlay: a floating waveform pill (tkinter).

A small dark pill at the bottom-center of the screen that exists only
while dictating. For the first ~2.5 s it also names the microphone
("Using <device>"), then collapses to just the waveform. The bars react
to the live mic level while listening, and switch to distinct *motion*
as well as color for the later states, so no state is conveyed by color
alone:

    listening     level-reactive bars, off-white
    transcribing  a bump sweeping across the bars, amber
    polishing     all bars breathing together, violet
    delivering    one short full-height flash, green
    error/idle    the pill disappears (beeps/notifications carry detail)

Colors are the tray palette brightened for the near-black pill.

Design constraints, in priority order:
- never steal focus or eat keystrokes: the pill is an override-redirect
  window, which takes no input focus — dictation aims at another app;
- zero new dependencies: tkinter ships with Python;
- degrade silently: no DISPLAY, no Tk module, macOS (where Tk must own
  the main thread the tray already holds) — one stderr line, dictation
  untouched;
- Wayland: Tk has no Wayland backend, so the pill is an XWayland
  override-redirect window — the one stdlib way a Python app can place
  a self-positioned always-on-top widget under GNOME/KDE Wayland.

All state/animation logic lives in OverlayModel — plain math with
injected timestamps, fully headless-testable. Only _OverlayView touches
Tk, and every Tk call happens on the thread that runs run_overlay().
"""

from __future__ import annotations

import math
import queue
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import events

if TYPE_CHECKING:
    from .config import Config
    from .events import StateBus, StateEvent

CAPTION_S = 2.5  # how long the "Using <mic>" caption stays up
DONE_FLASH_S = 0.4  # the green "delivered" flash
BAR_COUNT = 13

# Overlay modes (not 1:1 with bus states: delivering->done outlives IDLE).
HIDDEN = "hidden"
LIVE = "live"
TRANSCRIBING = "transcribing"
POLISHING = "polishing"
DONE = "done"

# An utterance at this RMS (loud, close speech) fills a bar completely;
# sqrt scaling below keeps normal speech (~0.02-0.08) visibly bouncing.
_FULL_RMS = 0.18
_BAR_FLOOR = 0.08  # resting bar height, so the pill never looks dead

_COLORS = {
    LIVE: "#f2f2f2",
    TRANSCRIBING: "#e8a33d",  # tray amber, brightened
    POLISHING: "#a78bfa",  # tray violet, brightened
    DONE: "#4ade80",  # tray green, brightened
}

_GENERIC_DEVICE_NAMES = {"", "default", "sysdefault", "pulse", "pipewire"}


class OverlayModel:
    """State machine + animation math for the pill. No Tk, time injected."""

    def __init__(
        self,
        caption: str,
        *,
        bar_count: int = BAR_COUNT,
        caption_s: float = CAPTION_S,
        done_flash_s: float = DONE_FLASH_S,
    ):
        self.caption = caption
        self.bar_count = bar_count
        self.caption_s = caption_s
        self.done_flash_s = done_flash_s
        self.mode = HIDDEN
        self._caption_until = 0.0
        self._done_until = 0.0
        self._phase = 0.0
        self._levels: deque[float] = deque([0.0] * bar_count, maxlen=bar_count)

    def on_state(self, state: str, now: float) -> None:
        """Feed a StateBus state; `now` must match the clock given to tick()."""
        if state == events.LISTENING:
            self.mode = LIVE
            self._caption_until = now + self.caption_s
            self._levels = deque([0.0] * self.bar_count, maxlen=self.bar_count)
        elif state == events.TRANSCRIBING:
            self.mode = TRANSCRIBING
        elif state == events.POLISHING:
            self.mode = POLISHING
        elif state == events.DELIVERING:
            self.mode = DONE
            self._done_until = now + self.done_flash_s
        elif state == events.IDLE and self.mode == DONE and now < self._done_until:
            pass  # IDLE lands right after DELIVERING; let the flash finish
        else:  # idle / error / stopped / unknown
            self.mode = HIDDEN

    def tick(self, now: float, level: float = 0.0) -> bool:
        """Advance one animation frame. Returns True while the pill shows."""
        self._phase = (self._phase + 0.02) % 1.0
        if self.mode == LIVE:
            self._levels.append(level)
        elif self.mode == DONE and now >= self._done_until:
            self.mode = HIDDEN
        return self.mode != HIDDEN

    def caption_visible(self, now: float) -> bool:
        return self.mode == LIVE and now < self._caption_until

    def color(self) -> str:
        return _COLORS.get(self.mode, _COLORS[LIVE])

    def bars(self) -> list[float]:
        """Per-bar heights in [0, 1], left to right."""
        n = self.bar_count
        if self.mode == LIVE:
            return [self._scale(lvl) for lvl in self._levels]
        if self.mode == TRANSCRIBING:
            # One bump travelling across the bars (~0.4 s per pass).
            return [
                max(
                    _BAR_FLOOR,
                    math.sin(math.pi * ((i / n + self._phase * 2.5) % 1.0)) ** 4,
                )
                for i in range(n)
            ]
        if self.mode == POLISHING:
            # Every bar breathing in unison (~1.6 s cycle).
            breath = 0.5 + 0.5 * math.sin(2 * math.pi * self._phase)
            return [max(_BAR_FLOOR, 0.15 + 0.55 * breath)] * n
        if self.mode == DONE:
            return [1.0] * n
        return [0.0] * n

    @staticmethod
    def _scale(level: float) -> float:
        """Map an RMS level to a bar height; sqrt keeps quiet speech alive."""
        return max(_BAR_FLOOR, min(1.0, math.sqrt(max(0.0, level) / _FULL_RMS)))


def device_label(cfg: Config, query_devices: Callable | None = None) -> str:
    """The caption text: "Using <mic name>", resolved like the Recorder.

    Mirrors audio._normalize_device: "" means the system default, a digit
    string is a device index. Generic ALSA/Pulse aliases ("default",
    "pipewire", ...) are resolved to the real hardware name via
    sounddevice when possible. `query_devices` is injectable for tests.
    """
    name = str(cfg.audio.input_device).strip()
    if query_devices is None:
        try:
            import sounddevice as sd  # lazy, like audio.py

            query_devices = sd.query_devices
        except Exception:
            query_devices = None
    if query_devices is not None and (
        name.isdigit() or name.lower() in _GENERIC_DEVICE_NAMES
    ):
        try:
            if name.isdigit():
                info = query_devices(int(name))
            else:
                info = query_devices(kind="input")
            name = str(info["name"]).strip()
        except Exception:
            pass  # keep whatever we had; the caption is decoration
    if name.lower() in _GENERIC_DEVICE_NAMES:
        return "Using the default mic"
    if len(name) > 38:
        name = name[:37] + "…"
    return f"Using {name}"


def parse_xrandr_primary(output: str) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the primary monitor from `xrandr --query` output.

    Falls back to the first connected monitor with a geometry when none is
    marked primary; None when nothing parses (Tk's virtual screen wins).
    """
    fallback: tuple[int, int, int, int] | None = None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "connected":
            continue  # "disconnected" lines never match this exact token
        primary = parts[2] == "primary"
        match = re.search(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", line)
        if match is None:
            continue  # connected but inactive output: no geometry
        w, h, x, y = (int(g) for g in match.groups())
        if primary:
            return (x, y, w, h)
        if fallback is None:
            fallback = (x, y, w, h)
    return fallback


def run_overlay(
    cfg: Config,
    bus: StateBus,
    level_source: Callable[[], float] | None = None,
) -> None:
    """Run the overlay until the bus says STOPPED (blocking).

    Call in a daemon thread, like run_tray. `level_source` returns the
    current mic RMS (DictationApp.audio_level). Every failure is one
    stderr line — the overlay is decoration, never plumbing.
    """
    if sys.platform == "darwin":
        # Tk demands the macOS main thread, which the pystray AppKit tray
        # already owns (see cli.run's darwin inversion). Skip rather than
        # abort the process.
        print(
            "voicisst overlay: not supported on macOS yet — running without it",
            file=sys.stderr,
        )
        return
    inbox: queue.Queue[StateEvent] = queue.Queue()
    sub_id = bus.subscribe(inbox.put)
    try:
        model = OverlayModel(device_label(cfg))
        view = _OverlayView(model, level_source or (lambda: 0.0), inbox)
        view.run()
    except Exception as e:
        print(
            f"voicisst overlay: unavailable ({e}) — running without it",
            file=sys.stderr,
        )
    finally:
        bus.unsubscribe(sub_id)


class _OverlayView:
    """The Tk pill window. Construct and run on the same thread."""

    PILL_H = 36
    PILL_BG = "#161616"
    CAPTION_FG = "#d9d9d9"
    BAR_W = 3
    BAR_GAP = 3
    MAX_BAR_H = 22
    PAD_X = 16
    CAPTION_GAP = 12  # caption <-> waveform spacing
    MARGIN_BOTTOM = 64  # clearance above docks/panels
    TICK_MS = 33  # ~30 fps
    # Windows supports -transparentcolor: pixels in KEY vanish, giving a
    # true rounded pill. Elsewhere the window is the (rectangular) pill.
    KEY = "#0a0b0c"

    def __init__(
        self,
        model: OverlayModel,
        level_source: Callable[[], float],
        inbox: queue.Queue,
    ):
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.model = model
        self.level_source = level_source
        self.inbox = inbox
        self._time = time.monotonic

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)  # no decorations, no focus steal
        for attr, value in (("-topmost", True), ("-alpha", 0.94)):
            try:
                self.root.attributes(attr, value)
            except tk.TclError:
                pass
        bg = self.PILL_BG
        self._shaped = False
        try:
            self.root.attributes("-transparentcolor", self.KEY)
            bg = self.KEY
            self._shaped = True
        except tk.TclError:
            pass
        self.root.configure(bg=bg)
        self.canvas = tk.Canvas(
            self.root, bg=bg, highlightthickness=0, bd=0, height=self.PILL_H
        )
        self.canvas.pack(fill="both", expand=True)

        self.font = tkfont.nametofont("TkDefaultFont").copy()
        self.font.configure(size=10)
        caption_w = self.font.measure(self.model.caption) if self.model.caption else 0

        n = self.model.bar_count
        self.wave_w = n * self.BAR_W + (n - 1) * self.BAR_GAP
        self.compact_w = self.wave_w + 2 * self.PAD_X
        self.full_w = self.compact_w + caption_w + self.CAPTION_GAP
        self._width = float(self.full_w)
        self._visible = False
        self._monitor = self._pick_monitor()

    def run(self) -> None:
        self.root.after(self.TICK_MS, self._tick)
        self.root.mainloop()

    # -- per-frame ----------------------------------------------------------

    def _tick(self) -> None:
        now = self._time()
        stopped = self._drain_events(now)
        if stopped:
            self.root.destroy()
            return
        level = self.level_source() if self.model.mode == LIVE else 0.0
        visible = self.model.tick(now, level)
        if visible:
            self._layout(now)
            self._draw(now)
        if visible and not self._visible:
            self.root.deiconify()
            self.root.lift()
        elif not visible and self._visible:
            self.root.withdraw()
        self._visible = visible
        self.root.after(self.TICK_MS, self._tick)

    def _drain_events(self, now: float) -> bool:
        stopped = False
        while True:
            try:
                ev = self.inbox.get_nowait()
            except queue.Empty:
                return stopped
            if ev.state == events.STOPPED:
                stopped = True
            self.model.on_state(ev.state, now)

    def _layout(self, now: float) -> None:
        target = self.full_w if self.model.caption_visible(now) else self.compact_w
        # Ease the pill toward its target width (caption collapse).
        self._width += (target - self._width) * 0.3
        if abs(target - self._width) < 1.5:
            self._width = float(target)
        w = int(round(self._width))
        mx, my, mw, mh = self._monitor
        x = mx + (mw - w) // 2
        y = my + mh - self.PILL_H - self.MARGIN_BOTTOM
        self.root.geometry(f"{w}x{self.PILL_H}+{x}+{y}")

    def _pick_monitor(self) -> tuple[int, int, int, int]:
        """(x, y, w, h) of the monitor the pill belongs on.

        Tk only knows the combined virtual screen, whose bottom-center
        lands on the bezel seam of a side-by-side multi-monitor setup.
        Under X11/XWayland, xrandr knows the real layout — prefer the
        primary monitor. Fall back to the virtual screen.
        """
        try:
            import subprocess

            out = subprocess.run(
                ["xrandr", "--query"], capture_output=True, text=True, timeout=2
            ).stdout
        except Exception:
            out = ""
        bbox = parse_xrandr_primary(out)
        if bbox is not None:
            return bbox
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _draw(self, now: float) -> None:
        c = self.canvas
        c.delete("all")
        w = int(round(self._width))
        h = self.PILL_H
        if self._shaped:
            self._rounded_pill(0, 0, w, h, h // 2, self.PILL_BG)
        # Caption sits left; the waveform hugs the right edge so it stays
        # put (becoming centered) while the pill collapses around it.
        caption = self.model.caption
        if caption and self.model.caption_visible(now) and w >= self.full_w - 2:
            c.create_text(
                self.PAD_X,
                h / 2,
                text=caption,
                anchor="w",
                fill=self.CAPTION_FG,
                font=self.font,
            )
        color = self.model.color()
        x = w - self.PAD_X - self.wave_w
        cy = h / 2
        for frac in self.model.bars():
            bar_h = max(2.0, frac * self.MAX_BAR_H)
            c.create_line(
                x + self.BAR_W / 2,
                cy - bar_h / 2,
                x + self.BAR_W / 2,
                cy + bar_h / 2,
                width=self.BAR_W,
                capstyle="round",
                fill=color,
            )
            x += self.BAR_W + self.BAR_GAP

    def _rounded_pill(self, x0: int, y0: int, x1: int, y1: int, r: int, fill: str) -> None:
        c = self.canvas
        c.create_oval(x0, y0, x0 + 2 * r, y1, fill=fill, outline=fill)
        c.create_oval(x1 - 2 * r, y0, x1, y1, fill=fill, outline=fill)
        c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
