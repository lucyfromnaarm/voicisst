"""Engine factory: pick local (in-process) or remote (voicisst server) inference."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Engine, EngineError, StreamSession

if TYPE_CHECKING:
    from ..config import Config

__all__ = ["Engine", "EngineError", "StreamSession", "get_engine"]


def get_engine(cfg: Config) -> Engine:
    """Build the engine selected by cfg.engine.mode ("local" | "remote")."""
    mode = (cfg.engine.mode or "local").strip().lower()
    if mode == "local":
        from .local import LocalEngine

        return LocalEngine(cfg)
    if mode == "remote":
        if not (cfg.engine.server_url or "").strip():
            raise EngineError(
                "engine mode is 'remote' but no server URL is configured",
                hint="pass --server http://<host>:8765 or set [engine] server_url "
                "in config.toml (run `voicisst config path` to find it)",
            )
        from .remote import RemoteEngine

        return RemoteEngine(cfg)
    raise EngineError(
        f"unknown engine mode {mode!r}",
        hint="set [engine] mode to 'local' or 'remote' in config.toml",
    )
