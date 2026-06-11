"""Local web UI: onboarding wizard, settings editor, dictation dashboard.

`create_ui_app` builds the FastAPI app (engine/bus injected, tests pass
fakes); `serve_ui` is the blocking `voicisst ui` entry point. The frontend
lives in `static/` as plain hand-written HTML/CSS/JS package data.
"""

from __future__ import annotations

from .server import create_ui_app, serve_ui

__all__ = ["create_ui_app", "serve_ui"]
