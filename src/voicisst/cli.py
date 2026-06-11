"""Voicisst command-line interface: `voicisst run / serve / ui / selftest / config / version`.

Bare `voicisst` is `voicisst run`. CLI flags map onto config overrides (the highest
precedence layer in load_config), so `--server URL` is exactly
`engine.mode=remote` + `engine.server_url=URL`, etc.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from pathlib import Path

import click

from . import __version__
from . import config as config_mod
from .dictation import DictationApp
from .engine import EngineError, get_engine

# exists=True: a typo'd --config must error (exit 2), not silently fall back
# to defaults — load_config tolerates a missing *default* path by design.
_CONFIG_OPTION = click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    metavar="PATH",
    help="Config file to use (default: `voicisst config path`).",
)


def _load_serve_ui():
    """Import the web UI server lazily; explain the fix when it's missing.

    The UI stack (fastapi/uvicorn) is an optional extra; a missing import
    must read as "here is how to install it", never as a traceback.
    """
    try:
        from .ui.server import serve_ui
    except ImportError as e:
        raise EngineError(
            f"the web UI is not available: {e}",
            hint="install the UI extra: pip install 'voicisst[ui]'",
        ) from e
    return serve_ui


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Voicisst — free, open-source voice dictation.

    Hold the hotkey, speak, release: clear, polished text appears in
    whatever app has focus. Run with no command to start dictating.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


@cli.command()
@click.option(
    "--server",
    "server_url",
    default=None,
    metavar="URL",
    help="Use a remote `voicisst serve` instance (e.g. http://big-box:8765).",
)
@click.option("--token", default=None, help="Bearer token for the remote server.")
@click.option(
    "--stream/--no-stream",
    "stream",
    default=None,
    help="Live-type the transcript while you speak.",
)
@click.option("--toggle", is_flag=True, help="Tap to start/stop instead of hold-to-talk.")
@click.option(
    "--language",
    default=None,
    metavar="LANG",
    help='Force a language ("en", "es", ...); default auto-detects.',
)
@_CONFIG_OPTION
@click.option("--tray", is_flag=True, help="Show a system-tray icon (needs the tray extra).")
@click.option(
    "--ui",
    "with_ui",
    is_flag=True,
    help="Also serve the local web dashboard/settings UI (see `voicisst ui`).",
)
def run(
    server_url: str | None,
    token: str | None,
    stream: bool | None,
    toggle: bool,
    language: str | None,
    config_file: Path | None,
    tray: bool,
    with_ui: bool,
) -> None:
    """Run dictation (the default command)."""
    overrides: dict[str, object] = {}
    if server_url:
        overrides["engine.mode"] = "remote"
        overrides["engine.server_url"] = server_url
    if token is not None:
        overrides["engine.token"] = token
    if stream is not None:
        overrides["output.stream"] = stream
    if toggle:
        overrides["hotkey.mode"] = "toggle"
    if language:
        overrides["whisper.language"] = language
    if tray:
        overrides["ui.tray"] = True
    cfg = config_mod.load_config(path=config_file, overrides=overrides)
    engine = get_engine(cfg)

    bus = None
    ui_url: str | None = None
    if with_ui:
        # One StateBus shared by the dictation app, the web dashboard and
        # the tray: everyone sees the same live state.
        from .events import StateBus

        serve_ui = _load_serve_ui()
        bus = StateBus()
        # serve_ui generates the per-run token and prints/open()s the full
        # tokened URL itself; the tray gets the base URL — the browser's
        # cookie from that first launch authorizes it.
        ui_url = f"http://127.0.0.1:{cfg.ui.web_port}/"
        # uvicorn runs in a daemon thread with its own event loop; dictation
        # keeps the main thread (except the darwin tray inversion below).
        threading.Thread(
            target=serve_ui,
            args=(cfg,),
            kwargs={"bus": bus},
            name="voicisst-ui",
            daemon=True,
        ).start()

    app = DictationApp(cfg, engine, bus=bus)
    if not cfg.ui.tray:
        app.run()
        return
    from .tray import run_tray

    # Only pass the new tray params when the UI is on, so a plain
    # `voicisst run --tray` keeps its exact pre-UI call shape.
    tray_kwargs: dict[str, object] = {"bus": bus, "ui_url": ui_url} if with_ui else {}
    if sys.platform == "darwin":
        # pystray's AppKit backend must own the MAIN thread on macOS, so the
        # arrangement is inverted there: dictation runs in a background thread
        # and the tray blocks here. When the tray exits (Quit or Ctrl+C) the
        # app is stopped so its teardown still runs.
        app_thread = threading.Thread(target=app.run, name="voicisst", daemon=True)
        app_thread.start()
        try:
            run_tray(app, cfg, **tray_kwargs)
        except KeyboardInterrupt:
            print("\nflow: shutting down", file=sys.stderr)
        finally:
            app.stop()
            app_thread.join(timeout=5.0)
        return
    threading.Thread(
        target=run_tray, args=(app, cfg), kwargs=tray_kwargs, name="voicisst-tray", daemon=True
    ).start()
    app.run()


@cli.command()
@click.option("--host", default=None, help="Bind address (default 127.0.0.1).")
@click.option("--port", type=int, default=None, help="Port (default 8765).")
@click.option("--token", default=None, help="Require this bearer token from clients.")
@_CONFIG_OPTION
def serve(
    host: str | None, port: int | None, token: str | None, config_file: Path | None
) -> None:
    """Run the transcription/polish server (for the big-GPU box)."""
    overrides: dict[str, object] = {}
    if host:
        overrides["server.host"] = host
    if port is not None:
        overrides["server.port"] = port
    if token is not None:
        overrides["server.token"] = token
    cfg = config_mod.load_config(path=config_file, overrides=overrides)
    from . import server as server_mod

    server_mod.serve(cfg)


@cli.command("ui")
@click.option(
    "--port",
    type=int,
    default=None,
    help="Port for the web UI (default: [ui] web_port, 8766).",
)
@click.option(
    "--no-browser",
    "no_browser",
    is_flag=True,
    help="Don't open the browser automatically; just print the URL.",
)
@_CONFIG_OPTION
def ui(port: int | None, no_browser: bool, config_file: Path | None) -> None:
    """Open the setup and settings web UI (no dictation).

    Serves on 127.0.0.1 only. For the live dictation dashboard, use
    `voicisst run --ui` instead.
    """
    overrides: dict[str, object] = {}
    if port is not None:
        overrides["ui.web_port"] = port
    cfg = config_mod.load_config(path=config_file, overrides=overrides)
    serve_ui = _load_serve_ui()
    # open_browser=None lets [ui] open_browser from the config decide.
    serve_ui(cfg, open_browser=False if no_browser else None, port=port)


@cli.command()
@click.option(
    "--server",
    "server_url",
    default=None,
    metavar="URL",
    help="Also check a remote voicisst server instead of the local engine.",
)
@_CONFIG_OPTION
@click.pass_context
def selftest(ctx: click.Context, server_url: str | None, config_file: Path | None) -> None:
    """Diagnose the environment: hotkeys, audio, engine, injection, clipboard."""
    overrides: dict[str, object] = {}
    if server_url:
        overrides["engine.mode"] = "remote"
        overrides["engine.server_url"] = server_url
    cfg = config_mod.load_config(path=config_file, overrides=overrides)
    from .selftest import run_selftest

    ctx.exit(run_selftest(cfg))


@cli.group("config")
def config_group() -> None:
    """Manage the Voicisst config file."""


@config_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def config_init(force: bool) -> None:
    """Write a documented config template to the default location."""
    path = config_mod.config_path()
    if path.exists() and not force:
        raise click.ClickException(
            f"{path} already exists — pass --force to overwrite it"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod.default_config_toml(), encoding="utf-8")
    click.echo(f"wrote config template to {path}")


@config_group.command("show")
@_CONFIG_OPTION
def config_show(config_file: Path | None) -> None:
    """Print the effective configuration (file + env + defaults)."""
    cfg = config_mod.load_config(path=config_file)
    click.echo(_format_config(cfg))


@config_group.command("path")
def config_path_cmd() -> None:
    """Print where Voicisst looks for its config file."""
    click.echo(str(config_mod.config_path()))


@cli.command()
def version() -> None:
    """Print the Voicisst version."""
    click.echo(f"voicisst {__version__}")


# ---------------------------------------------------------------------------


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def _format_config(cfg: config_mod.Config) -> str:
    """TOML-ish dump of the effective config."""
    lines: list[str] = []
    for section_field in dataclasses.fields(cfg):
        value = getattr(cfg, section_field.name)
        lines.append(f"[{section_field.name}]")
        if section_field.name == "replacements":
            for key, repl in value.items():
                lines.append(f"{json.dumps(str(key))} = {json.dumps(str(repl))}")
        else:
            for f in dataclasses.fields(value):
                lines.append(f"{f.name} = {_toml_value(getattr(value, f.name))}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    """Console entry point (`voicisst`)."""
    try:
        rv = cli.main(standalone_mode=False)
    except EngineError as e:
        print(f"voicisst: {e}", file=sys.stderr)
        if getattr(e, "hint", ""):
            print(f"  hint: {e.hint}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except click.exceptions.Abort:
        print("Aborted!", file=sys.stderr)
        sys.exit(1)
    if isinstance(rv, int) and rv != 0:
        sys.exit(rv)
