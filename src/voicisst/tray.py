"""Optional system-tray icon via pystray (the `tray` extra).

`run_tray(app, cfg)` blocks until Quit is chosen or the icon backend dies.
pystray/Pillow are imported lazily; if either is missing the user gets a
notify() hint instead of a crash — the tray is decoration, not plumbing.

When a `bus` (events.StateBus) is passed, the icon mirrors the dictation
state. Every state gets a DISTINCT SHAPE as well as a color, so the states
are still distinguishable for colorblind users or monochrome tray themes:

    idle/stopped  hollow gray ring
    listening     filled red circle
    transcribing  half-filled amber circle
    polishing     violet diamond
    delivering    green check mark
    error         white X on a black disc (high contrast)
"""

from __future__ import annotations

import sys
import webbrowser
from typing import TYPE_CHECKING, Any

from . import events
from .notify import notify

if TYPE_CHECKING:
    from .config import Config
    from .dictation import DictationApp
    from .events import StateBus, StateEvent

_INSTALL_HINT = "tray extra not installed: pip install 'voicisst[tray]'"

_SIZE = 64

# Colors chosen to match the web dashboard's state palette.
_GRAY = (128, 128, 128, 255)
_RED = (214, 40, 40, 255)
_AMBER = (224, 140, 0, 255)
_VIOLET = (124, 58, 237, 255)
_GREEN = (22, 163, 74, 255)
_BLACK = (10, 10, 10, 255)
_WHITE = (255, 255, 255, 255)


def _draw_state_icon(state: str) -> Any:
    """One 64px RGBA icon per state: distinct shape AND color (never
    color alone)."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if state == events.LISTENING:
        # Filled red circle: recording live.
        draw.ellipse((8, 8, 56, 56), fill=_RED)
    elif state == events.TRANSCRIBING:
        # Half-filled amber circle: turning speech into text.
        draw.ellipse((8, 8, 56, 56), outline=_AMBER, width=6)
        draw.pieslice((8, 8, 56, 56), 90, 270, fill=_AMBER)
    elif state == events.POLISHING:
        # Violet diamond: the polish window (Backspace cancels).
        draw.polygon([(32, 4), (60, 32), (32, 60), (4, 32)], fill=_VIOLET)
    elif state == events.DELIVERING:
        # Green check: text is going into the focused app.
        draw.line([(10, 34), (26, 50), (54, 14)], fill=_GREEN, width=10)
    elif state == events.ERROR:
        # White X on a black disc: maximum contrast.
        draw.ellipse((2, 2, 62, 62), fill=_BLACK)
        draw.line([(20, 20), (44, 44)], fill=_WHITE, width=9)
        draw.line([(44, 20), (20, 44)], fill=_WHITE, width=9)
    else:
        # idle / stopped / anything unknown: hollow gray ring.
        draw.ellipse((8, 8, 56, 56), outline=_GRAY, width=7)
    return image


def _state_images() -> dict[str, Any]:
    """Pre-rendered icon for every bus state (PIL imported lazily)."""
    return {state: _draw_state_icon(state) for state in events.ALL_STATES}


def _latin1_safe(text: str) -> str:
    """pystray's X11 backend encodes titles/tooltips as latin-1; replace
    anything outside it (em-dashes, emoji in transcript details) instead
    of letting the encode error kill the update."""
    return text.encode("latin-1", "replace").decode("latin-1")


def run_tray(
    app: DictationApp,
    cfg: Config,
    bus: StateBus | None = None,
    ui_url: str | None = None,
) -> None:
    """Run the tray icon (blocking). Safe to call in a daemon thread.

    `bus`: subscribe and mirror the dictation state in the icon + title.
    `ui_url`: adds an "Open settings UI" menu item that opens the web UI.
    Every failure here is non-fatal — dictation keeps running without us.
    """
    try:
        import pystray
    except ImportError:
        notify("tray unavailable", _INSTALL_HINT, enabled=cfg.ui.notify)
        return
    try:
        images = _state_images()
    except ImportError:
        notify("tray unavailable", _INSTALL_HINT, enabled=cfg.ui.notify)
        return

    def status(_item: Any) -> str:
        target = cfg.engine.server_url if cfg.engine.mode == "remote" else cfg.whisper.model
        return _latin1_safe(f"Voicisst — {cfg.engine.mode} / {target} ({cfg.hotkey.mode})")

    def toggle_polish(_icon: Any, _item: Any) -> None:
        cfg.polish.enabled = not cfg.polish.enabled
        state = "enabled" if cfg.polish.enabled else "disabled"
        notify(f"polish {state}", enabled=cfg.ui.notify)

    def do_quit(icon: Any, _item: Any) -> None:
        app.stop()
        icon.stop()

    items = [pystray.MenuItem(status, None, enabled=False)]
    if ui_url:

        def open_ui(_icon: Any, _item: Any) -> None:
            try:
                webbrowser.open(ui_url)
            except Exception as e:
                print(f"voicisst tray: could not open {ui_url}: {e}", file=sys.stderr)

        items.append(pystray.MenuItem("Open settings UI", open_ui))
    items.append(
        pystray.MenuItem("Polish", toggle_polish, checked=lambda _item: cfg.polish.enabled)
    )
    items.append(pystray.MenuItem("Quit Voicisst", do_quit))

    icon = pystray.Icon(
        "voicisst", images[events.IDLE], "Voicisst dictation", menu=pystray.Menu(*items)
    )

    sub_id: int | None = None
    if bus is not None:

        def on_event(event: StateEvent) -> None:
            # Runs on the publisher's (worker) thread: keep it instant and
            # never let a tray hiccup reach dictation.
            try:
                icon.icon = images.get(event.state, images[events.IDLE])
                title = f"Voicisst — {event.state}"
                if event.detail:
                    title = f"{title}: {event.detail}"
                icon.title = _latin1_safe(title)
            except Exception as e:
                print(f"voicisst tray: icon update failed: {e}", file=sys.stderr)

        sub_id = bus.subscribe(on_event)

    try:
        icon.run()
    except Exception as e:
        print(
            f"voicisst tray: could not start ({e}) — running without a tray icon",
            file=sys.stderr,
        )
    finally:
        if bus is not None and sub_id is not None:
            bus.unsubscribe(sub_id)
