"""Optional system-tray icon via pystray (the `tray` extra).

`run_tray(app, cfg)` blocks until Quit is chosen or the icon backend dies.
pystray/Pillow are imported lazily; if either is missing the user gets a
notify() hint instead of a crash — the tray is decoration, not plumbing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .notify import notify

if TYPE_CHECKING:
    from .config import Config
    from .dictation import DictationApp

_INSTALL_HINT = "tray extra not installed: pip install 'flow-dictation[tray]'"


def _make_icon_image() -> Any:
    """A simple generated icon: filled circle with a 'mic dot' center."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, size - 6, size - 6), fill=(64, 132, 244, 255))
    draw.ellipse((24, 24, size - 24, size - 24), fill=(255, 255, 255, 255))
    return image


def run_tray(app: DictationApp, cfg: Config) -> None:
    """Run the tray icon (blocking). Safe to call in a daemon thread."""
    try:
        import pystray
    except ImportError:
        notify("tray unavailable", _INSTALL_HINT, enabled=cfg.ui.notify)
        return
    try:
        image = _make_icon_image()
    except ImportError:
        notify("tray unavailable", _INSTALL_HINT, enabled=cfg.ui.notify)
        return

    def status(_item: Any) -> str:
        target = cfg.engine.server_url if cfg.engine.mode == "remote" else cfg.whisper.model
        return f"Flow — {cfg.engine.mode} / {target} ({cfg.hotkey.mode})"

    def toggle_polish(_icon: Any, _item: Any) -> None:
        cfg.polish.enabled = not cfg.polish.enabled
        state = "enabled" if cfg.polish.enabled else "disabled"
        notify(f"polish {state}", enabled=cfg.ui.notify)

    def do_quit(icon: Any, _item: Any) -> None:
        app.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status, None, enabled=False),
        pystray.MenuItem("Polish", toggle_polish, checked=lambda _item: cfg.polish.enabled),
        pystray.MenuItem("Quit Flow", do_quit),
    )
    icon = pystray.Icon("flow-dictation", image, "Flow dictation", menu=menu)
    try:
        icon.run()
    except Exception as e:
        print(f"flow tray: could not start ({e}) — running without a tray icon", file=sys.stderr)
