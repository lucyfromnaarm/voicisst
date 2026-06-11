"""Local web UI server: onboarding wizard, settings editor, live dashboard.

Served on 127.0.0.1 only, guarded by a per-run token (see docs/UI-SPEC.md
"Security model"). The engine is optional and lazily built so the settings
pages work even when no model is installed yet.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import os
import secrets
import select
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import __version__, events
from .. import audio as audio_mod
from .. import config as config_mod
from ..engine.base import Engine, EngineError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..config import Config
    from ..events import StateBus

__all__ = ["create_ui_app", "serve_ui"]

_INSTALL_HINT = "install the ui extra: pip install 'voicisst[ui]'"
COOKIE_NAME = "voicisst_ui"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# A deliberately messy sample so "Test polish" visibly cleans something up.
_DEFAULT_POLISH_SAMPLE = (
    "um so basically i think we should, uh, meet at 2... actually 3 pm, you know"
)

_FORBIDDEN_JSON = {
    "error": "missing or wrong UI token",
    "hint": "open the http://127.0.0.1:<port>/?t=<token> link voicisst printed "
    "when it started",
}

_FORBIDDEN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Voicisst — use your link</title></head>
<body>
<h1>This page needs its key</h1>
<p>For your safety, the Voicisst settings page only opens from the special
link Voicisst prints when it starts.</p>
<p>Go back to the terminal where Voicisst is running and open the link that
looks like <code>http://127.0.0.1:8766/?t=...</code>.</p>
</body></html>"""

_PLACEHOLDER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Voicisst</title></head>
<body>
<h1>Voicisst</h1>
<p>The Voicisst UI server is running, but its page files are missing from
this install. The API still works. To fix the pages, reinstall with:
<code>pip install 'voicisst[ui]'</code></p>
</body></html>"""


# ---------------------------------------------------------------------------
# Config file helpers (shared by the PUT endpoints)


def write_config_atomic(path: Path, text: str) -> None:
    """Atomically write `text` to `path` with 0600 permissions.

    Same-directory temp file + os.replace, so a crash mid-write never
    leaves a half-written config; mkstemp creates the file 0600.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _require_tomlkit() -> Any:
    try:
        import tomlkit
    except ImportError as e:
        raise EngineError("tomlkit is not installed", hint=_INSTALL_HINT) from e
    return tomlkit


def _validate_config_text(text: str) -> str:
    """Return "" when `text` is a loadable voicisst config, else an error."""
    tomlkit = _require_tomlkit()
    try:
        tomlkit.parse(text)
    except Exception as e:
        return f"that is not valid TOML: {e}"
    fd, tmp_name = tempfile.mkstemp(suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        config_mod.load_config(path=tmp_name, env={})
    except Exception as e:
        return f"the config could not be loaded: {e}"
    finally:
        with suppress(OSError):
            os.unlink(tmp_name)
    return ""


def _set_replacements(tomlkit: Any, doc: Any, key: str, value: object) -> str:
    """Apply a replacements update to the tomlkit document. "" on success."""
    if "replacements" not in doc:
        doc["replacements"] = tomlkit.table()
    table = doc["replacements"]
    if key:  # dotted form: "replacements.vs code" = "VS Code"
        table[key] = str(value)
        return ""
    if not isinstance(value, Mapping):
        return 'replacements must be an object of {"spoken": "written"} pairs'
    for old in list(table.keys()):
        if str(old) not in value:
            del table[old]
    for k, v in value.items():
        table[str(k)] = str(v)
    return ""


def _apply_values(text: str, values: Mapping[str, object]) -> tuple[str, str]:
    """Set dotted keys on the TOML text, preserving comments and layout.

    Returns (new_text, "") or ("", error). Unknown sections/keys produce
    config.py's did-you-mean suggestions.
    """
    tomlkit = _require_tomlkit()
    try:
        doc = tomlkit.parse(text)
    except Exception as e:
        return "", f"the existing config file is not valid TOML: {e}"
    for dotted, value in values.items():
        section_name, _, key = str(dotted).partition(".")
        if section_name == "replacements":
            error = _set_replacements(tomlkit, doc, key, value)
            if error:
                return "", error
            continue
        section_cls = config_mod._SECTIONS.get(section_name)
        if section_cls is None:
            hint = config_mod._suggest(section_name, [*config_mod._SECTIONS, "replacements"])
            return "", f"unknown config section {section_name!r}{hint}"
        if not key:
            return "", (
                f"{section_name!r} needs a dotted key like "
                f"'{section_name}.<setting>'"
            )
        types = config_mod._field_types(section_cls)
        if key not in types:
            hint = config_mod._suggest(key, list(types))
            return "", f"unknown config key {dotted!r}{hint}"
        try:
            coerced = config_mod._coerce(value, types[key])
        except (ValueError, TypeError) as e:
            return "", f"bad value for {dotted!r}: {e}"
        try:
            if section_name not in doc:
                doc[section_name] = tomlkit.table()
            doc[section_name][key] = coerced
        except Exception as e:
            return "", f"could not set {dotted!r}: {e}"
    return tomlkit.dumps(doc), ""


def _config_values(cfg: Config) -> dict[str, Any]:
    """The effective config as {section: {key: value}} (plain JSON types)."""
    out: dict[str, Any] = {}
    for section_field in dataclasses.fields(cfg):
        value = getattr(cfg, section_field.name)
        if section_field.name == "replacements":
            out["replacements"] = dict(value)
        else:
            out[section_field.name] = {
                f.name: getattr(value, f.name) for f in dataclasses.fields(value)
            }
    return out


# ---------------------------------------------------------------------------
# One-shot hotkey capture (the setup wizard's "Press a key…" button)


def _close_quietly(dev: Any) -> None:
    try:
        dev.close()
    except Exception:
        pass


def _capture_evdev(timeout_s: float) -> str:
    """Wait for the next key press on any keyboard; return its evdev name.

    Devices are opened read-only and NON-exclusively (the key still reaches
    the focused app) and are always closed, even on timeout.
    """
    try:
        import evdev
        from evdev import ecodes
    except ImportError as e:
        raise EngineError(
            "evdev is not installed", hint="pip install evdev (Linux only)"
        ) from e
    from ..hotkeys.evdev_listener import INPUT_GROUP_HINT

    devices: list[Any] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)  # plain open: no grab, nothing stolen
        except (PermissionError, OSError):
            continue
        if "ydotool" in (dev.name or "").lower():  # our own virtual keyboard
            _close_quietly(dev)
            continue
        if not dev.capabilities().get(ecodes.EV_KEY):
            _close_quietly(dev)
            continue
        devices.append(dev)
    if not devices:
        raise EngineError(
            "no readable keyboard found under /dev/input", hint=INPUT_GROUP_HINT
        )
    try:
        fd_map = {dev.fd: dev for dev in devices}
        deadline = time.monotonic() + timeout_s
        while fd_map:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no key pressed within {timeout_s:g} seconds")
            try:
                readable, _, _ = select.select(list(fd_map), [], [], min(0.2, remaining))
            except (OSError, ValueError):
                # A device vanished mid-wait; drop the dead ones and go on.
                for fd in list(fd_map):
                    try:
                        select.select([fd], [], [], 0)
                    except (OSError, ValueError):
                        fd_map.pop(fd, None)
                continue
            for fd in readable:
                dev = fd_map.get(fd)
                if dev is None:
                    continue
                try:
                    batch = list(dev.read())
                except BlockingIOError:
                    continue
                except OSError:
                    fd_map.pop(fd, None)
                    continue
                for event in batch:
                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue
                    name = ecodes.KEY.get(event.code)
                    if isinstance(name, (list, tuple)):
                        name = name[0] if name else None
                    if name:
                        return str(name)
        raise TimeoutError("all keyboards disappeared while waiting for a key")
    finally:
        for dev in devices:
            _close_quietly(dev)


def _capture_pynput(timeout_s: float) -> str:
    """Wait for the next key press via a one-shot pynput listener."""
    try:
        from pynput import keyboard
    except Exception as e:
        raise EngineError(
            "pynput is not usable here",
            hint="pip install pynput; on macOS grant Input Monitoring permission",
        ) from e

    captured: list[str] = []
    done = threading.Event()

    def _on_press(key: Any, injected: bool = False) -> bool | None:
        if injected:  # never capture our own synthetic keystrokes
            return None
        name = _pynput_key_name(key)
        if name:
            captured.append(name)
            done.set()
            return False  # stop the listener
        return None

    listener = keyboard.Listener(on_press=_on_press, suppress=False)
    listener.start()
    try:
        if not done.wait(timeout_s):
            raise TimeoutError(f"no key pressed within {timeout_s:g} seconds")
    finally:
        with suppress(Exception):
            listener.stop()
    return captured[0]


def _pynput_key_name(key: Any) -> str:
    """Friendly pynput name: 'f9', 'alt_r', or a single character."""
    if key is None:
        return ""
    char = getattr(key, "char", None)
    if char:
        return str(char).lower()
    name = getattr(key, "name", None)
    if name:
        return str(name)
    return str(key)


def capture_hotkey(cfg: Config, timeout_s: float) -> tuple[str, str]:
    """Capture the NEXT key press; returns (key name, backend name).

    Backend choice mirrors hotkeys/get_listener: Linux "auto" prefers evdev
    (Wayland-safe) and falls back to pynput; macOS/Windows use pynput.
    Raises TimeoutError when nothing is pressed in time.
    """
    backend = (cfg.hotkey.backend or "auto").strip().lower()
    if backend == "auto":
        if sys.platform == "linux":
            from ..hotkeys.evdev_listener import EvdevListener

            backend = "evdev" if EvdevListener.available() else "pynput"
        else:
            backend = "pynput"
    if backend == "evdev":
        return _capture_evdev(timeout_s), "evdev"
    if backend == "pynput":
        return _capture_pynput(timeout_s), "pynput"
    raise EngineError(
        f"unknown hotkey backend {backend!r}",
        hint='hotkey.backend must be "auto", "evdev", or "pynput"',
    )


# ---------------------------------------------------------------------------
# App factory


def create_ui_app(
    cfg: Config,
    *,
    bus: StateBus | None = None,
    engine: Engine | None = None,
    token: str = "",
    config_file: Path | None = None,
) -> FastAPI:
    """Build the UI app. `token` guards every request; "" disables auth
    (bare-test convenience — serve_ui always generates one)."""
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise EngineError("fastapi is not installed", hint=_INSTALL_HINT) from e

    # FastAPI resolves endpoint annotations through *module* globals; these
    # names are imported lazily (hard rule), so publish them for
    # `from __future__ import annotations` to keep working.
    globals()["Request"] = Request
    globals()["WebSocket"] = WebSocket

    cfg_path = Path(config_file) if config_file is not None else config_mod.config_path()
    app = FastAPI(title="voicisst ui", version=__version__)

    # -- auth -----------------------------------------------------------------

    def _token_ok(supplied: str) -> bool:
        # Constant-time comparison: never leak the token via timing.
        return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

    def _authorized(request: Request) -> bool:
        if not token:
            return True
        candidates = (
            request.query_params.get("t", ""),
            request.cookies.get(COOKIE_NAME, ""),
            request.headers.get("x-voicisst-token", ""),
        )
        return any(supplied and _token_ok(supplied) for supplied in candidates)

    @app.middleware("http")
    async def _require_token(request: Request, call_next: Any) -> Any:
        if not _authorized(request):
            if request.url.path.startswith("/api"):
                return JSONResponse(dict(_FORBIDDEN_JSON), status_code=403)
            return HTMLResponse(_FORBIDDEN_PAGE, status_code=403)
        response = await call_next(request)
        if token and not _token_ok(request.cookies.get(COOKIE_NAME, "")):
            # First authorized load (via ?t= or header): pin the cookie so
            # follow-up fetch/WS calls just work without the query token.
            response.set_cookie(
                COOKIE_NAME, token, httponly=True, samesite="strict", path="/"
            )
        return response

    # -- shared helpers ---------------------------------------------------------

    async def _json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(
                status_code=400, detail=f"request body must be JSON: {e}"
            ) from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        return data

    def _error_503(e: Exception) -> JSONResponse:
        hint = getattr(e, "hint", "") or (
            "check the terminal where voicisst is running for details"
        )
        return JSONResponse({"error": str(e), "hint": hint}, status_code=503)

    engine_box: dict[str, Engine | None] = {"engine": engine}
    engine_lock = threading.Lock()

    def _resolve_engine() -> Engine:
        """Lazily build the engine on first use (UI must start without one)."""
        with engine_lock:
            if engine_box["engine"] is None:
                from ..engine import get_engine

                engine_box["engine"] = get_engine(cfg)
            return engine_box["engine"]

    warm_lock = threading.Lock()
    warm_state: dict[str, str] = {"status": "idle", "detail": ""}

    # -- pages -------------------------------------------------------------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_file = STATIC_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return HTMLResponse(_PLACEHOLDER_PAGE)

    # -- meta / config ---------------------------------------------------------

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "platform": sys.platform,
            "onboarded": cfg_path.is_file(),
            "config_path": str(cfg_path),
            "dictation_running": bus is not None,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            if cfg_path.is_file():
                text = cfg_path.read_text(encoding="utf-8")
            else:
                text = config_mod.default_config_toml()
            effective = config_mod.load_config(path=cfg_path)
            return {"toml": text, "values": _config_values(effective), "path": str(cfg_path)}

        return await run_in_threadpool(_read)

    @app.put("/api/config")
    async def put_config(request: Request) -> Any:
        data = await _json_body(request)
        text = data.get("toml")
        if not isinstance(text, str):
            return JSONResponse(
                {"error": 'body must be {"toml": "<the whole config file>"}'},
                status_code=400,
            )
        try:
            error = await run_in_threadpool(_validate_config_text, text)
            if error:
                return JSONResponse({"error": error}, status_code=400)
            await run_in_threadpool(write_config_atomic, cfg_path, text)
        except EngineError as e:  # tomlkit missing
            return _error_503(e)
        return {"ok": True}

    @app.put("/api/config/values")
    async def put_config_values(request: Request) -> Any:
        data = await _json_body(request)
        values = data.get("values")
        if not isinstance(values, dict) or not values:
            return JSONResponse(
                {"error": 'body must be {"values": {"audio.min_record_ms": 300, ...}}'},
                status_code=400,
            )

        def _work() -> str:
            if cfg_path.is_file():
                text = cfg_path.read_text(encoding="utf-8")
            else:
                text = config_mod.default_config_toml()
            new_text, error = _apply_values(text, values)
            if error:
                return error
            error = _validate_config_text(new_text)
            if error:
                return error
            write_config_atomic(cfg_path, new_text)
            return ""

        try:
            error = await run_in_threadpool(_work)
        except EngineError as e:  # tomlkit missing
            return _error_503(e)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        return {"ok": True}

    # -- live state ---------------------------------------------------------------

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        if bus is None:
            return events.StateEvent(events.IDLE).as_dict()
        return bus.last.as_dict()

    @app.websocket("/ws/state")
    async def ws_state(ws: WebSocket) -> None:
        supplied = ws.query_params.get("t", "") or ws.cookies.get(COOKIE_NAME, "")
        if token and not _token_ok(supplied):
            with suppress(Exception):
                await ws.close(code=4403)
            return
        await ws.accept()

        if bus is None:
            # No dictation running: report idle once, then hold the socket
            # open so the dashboard can show "dictation not running".
            with suppress(Exception):
                await ws.send_json(events.StateEvent(events.IDLE).as_dict())
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        break
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def _on_event(event: events.StateEvent) -> None:
            # Publishers run on dictation threads; hop onto the UI loop.
            with suppress(RuntimeError):  # loop already closed at shutdown
                loop.call_soon_threadsafe(queue.put_nowait, event.as_dict())

        async def _pump() -> None:
            while True:
                payload = await queue.get()
                try:
                    await ws.send_json(payload)
                except Exception:
                    return

        sub_id = bus.subscribe(_on_event)  # fires immediately with .last
        pump = asyncio.create_task(_pump())
        try:
            while True:  # server -> client only: receive() just tracks the close
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            bus.unsubscribe(sub_id)
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump

    # -- audio ------------------------------------------------------------------------

    @app.get("/api/audio/devices")
    async def audio_devices() -> dict[str, Any]:
        def _list() -> dict[str, Any]:
            import sounddevice as sd  # lazy: optional at runtime, absent on CI

            try:
                default_input = int(sd.default.device[0])
            except Exception:
                default_input = -1
            out = []
            for i, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                index = int(dev.get("index", i))
                out.append(
                    {
                        "index": index,
                        "name": str(dev.get("name", f"device {index}")),
                        "default": index == default_input,
                    }
                )
            return {"devices": out}

        try:
            return await run_in_threadpool(_list)
        except Exception as e:
            return {
                "devices": [],
                "error": str(e),
                "hint": "no microphone found — plug one in, or list devices with "
                "`python -m sounddevice`",
            }

    @app.post("/api/audio/test")
    async def audio_test(request: Request) -> Any:
        data = await _json_body(request)
        try:
            seconds = float(data.get("seconds", 1.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "'seconds' must be a number"}, status_code=400)
        seconds = min(max(seconds, 0.1), 5.0)

        def _record() -> dict[str, Any]:
            try:
                recorder = audio_mod.Recorder(
                    samplerate=cfg.audio.sample_rate, device=cfg.audio.input_device
                )
                recorder.start()
                time.sleep(seconds)
                buf, _duration_ms = recorder.stop()
            except Exception as e:
                return {"ok": False, "rms": 0.0, "peak": 0.0, "samples": 0, "hint": str(e)}
            level = audio_mod.rms(buf)
            peak = float(np.max(np.abs(buf))) if buf.size else 0.0
            result = {
                "ok": True,
                "rms": level,
                "peak": peak,
                "samples": int(buf.size),
                "hint": "",
            }
            if buf.size == 0:
                result["ok"] = False
                result["hint"] = (
                    "no audio arrived — is a microphone connected and selected? "
                    "Pick a device in settings ([audio] input_device)."
                )
            elif level <= cfg.audio.muted_rms:
                result["ok"] = False
                result["hint"] = (
                    f"the microphone looks muted (level {level:.6f} is at or below "
                    f"the muted threshold {cfg.audio.muted_rms:g}) — unmute it in "
                    "your system sound settings"
                )
            elif level < cfg.audio.rms_gate:
                result["hint"] = (
                    f"very quiet (level {level:.4f} is under the gate "
                    f"{cfg.audio.rms_gate:g}) — voicisst will boost it, but moving "
                    "closer to the microphone or raising input volume helps"
                )
            return result

        return await run_in_threadpool(_record)

    # -- hotkey capture -----------------------------------------------------------------

    @app.post("/api/hotkey/capture")
    async def hotkey_capture(request: Request) -> Any:
        data = await _json_body(request)
        try:
            timeout_s = float(data.get("timeout_s", 5.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "'timeout_s' must be a number"}, status_code=400)
        timeout_s = min(max(timeout_s, 0.1), 15.0)
        try:
            key, backend = await run_in_threadpool(capture_hotkey, cfg, timeout_s)
        except TimeoutError as e:
            return JSONResponse(
                {
                    "error": str(e) or "no key was pressed in time",
                    "hint": "click the capture button again, then press the key "
                    "you want to use",
                },
                status_code=408,
            )
        except Exception as e:
            return _error_503(e)
        return {"key": key, "backend": backend}

    # -- engine -------------------------------------------------------------------------

    @app.post("/api/engine/warm")
    async def engine_warm_start() -> Any:
        try:
            eng = await run_in_threadpool(_resolve_engine)
        except Exception as e:
            return _error_503(e)
        with warm_lock:
            if warm_state["status"] == "loading":
                return {"status": "loading"}
            warm_state["status"] = "loading"
            warm_state["detail"] = ""

        def _warm() -> None:
            try:
                eng.warm()
            except Exception as e:
                detail = str(e)
                hint = getattr(e, "hint", "")
                if hint:
                    detail = f"{detail} — {hint}"
                with warm_lock:
                    warm_state["status"] = "error"
                    warm_state["detail"] = detail
                return
            with warm_lock:
                warm_state["status"] = "ready"
                warm_state["detail"] = ""

        threading.Thread(target=_warm, name="voicisst-ui-warm", daemon=True).start()
        return {"status": "loading"}

    @app.get("/api/engine/warm")
    async def engine_warm_status() -> dict[str, str]:
        with warm_lock:
            return dict(warm_state)

    @app.get("/api/engine/health")
    async def engine_health() -> Any:
        try:
            eng = await run_in_threadpool(_resolve_engine)
            return await run_in_threadpool(eng.health)
        except Exception as e:
            return _error_503(e)

    # -- polish ----------------------------------------------------------------------------

    @app.post("/api/polish/test")
    async def polish_test(request: Request) -> Any:
        data = await _json_body(request)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            text = _DEFAULT_POLISH_SAMPLE
        try:
            eng = await run_in_threadpool(_resolve_engine)
            result = await run_in_threadpool(eng.polish, text)
        except Exception as e:
            return _error_503(e)
        return {"result": result, "changed": result != text}

    @app.get("/api/polish/models")
    async def polish_models(request: Request) -> Any:
        """List the models installed on the polish backend, for the model
        dropdown. `backend`/`url` query params let the settings form ask
        about values it has not saved yet."""
        backend = (
            request.query_params.get("backend") or cfg.polish.backend or ""
        ).strip().lower()
        url = (request.query_params.get("url") or cfg.polish.url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            return JSONResponse(
                {"error": f"polish.url must start with http:// or https:// (got {url!r})"},
                status_code=400,
            )

        def _list() -> dict[str, Any]:
            import requests  # lazy, mirrors the rest of this module

            from ..polish import LMSTUDIO_DEFAULT_URL, OLLAMA_DEFAULT_URL

            base = url
            hints = {
                "ollama": "is Ollama running? `ollama list` shows what is installed",
                "lmstudio": "in LM Studio, open the Developer tab and turn the "
                "local server on",
            }
            try:
                if backend == "ollama":
                    r = requests.get(f"{base}/api/tags", timeout=5)
                    r.raise_for_status()
                    names = [m.get("name", "") for m in r.json().get("models", [])]
                else:
                    # LM Studio and friends speak the OpenAI models API.
                    if backend == "lmstudio" and base == OLLAMA_DEFAULT_URL:
                        base = LMSTUDIO_DEFAULT_URL
                    headers = {}
                    if cfg.polish.api_key:
                        headers["Authorization"] = f"Bearer {cfg.polish.api_key}"
                    r = requests.get(f"{base}/v1/models", timeout=5, headers=headers)
                    r.raise_for_status()
                    names = [m.get("id", "") for m in r.json().get("data", [])]
            except (requests.RequestException, ValueError) as e:
                return {
                    "models": [],
                    "error": str(e),
                    "hint": hints.get(backend, f"no model list at {base} — check polish.url"),
                }
            return {"models": sorted({n for n in names if n})}

        return await run_in_threadpool(_list)

    return app


# ---------------------------------------------------------------------------
# Entry point


def serve_ui(
    cfg: Config,
    *,
    bus: StateBus | None = None,
    engine: Engine | None = None,
    open_browser: bool | None = None,
    port: int | None = None,
) -> None:
    """Run the UI server (blocking): per-run token, loud URL, uvicorn.

    Binds 127.0.0.1 ONLY — the token-in-URL scheme is what keeps other
    local users out, and loopback keeps the network out entirely.
    """
    try:
        import uvicorn
    except ImportError as e:
        raise EngineError("uvicorn is not installed", hint=_INSTALL_HINT) from e

    token = secrets.token_urlsafe(16)
    ui_port = cfg.ui.web_port if port is None else int(port)
    app = create_ui_app(cfg, bus=bus, engine=engine, token=token)
    url = f"http://127.0.0.1:{ui_port}/?t={token}"
    line = "=" * 68
    print(
        f"\n{line}\n"
        f"  Voicisst settings are ready. Open this link in your browser:\n"
        f"\n"
        f"    {url}\n"
        f"\n"
        f"  The link only works on this computer, and only for this run.\n"
        f"{line}\n",
        file=sys.stderr,
    )
    if open_browser is None:
        open_browser = cfg.ui.open_browser
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(
                f"voicisst ui: could not open a browser ({e}) — use the link above",
                file=sys.stderr,
            )
    uvicorn.run(app, host="127.0.0.1", port=ui_port, log_level="warning")
