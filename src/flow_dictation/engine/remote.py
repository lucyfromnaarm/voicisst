"""RemoteEngine: client side of the flow HTTP/WS protocol (`flow serve`)."""

from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

import numpy as np
import requests

from ..protocol import (
    MSG_CANCEL,
    MSG_ERROR,
    MSG_FINAL,
    MSG_FINALIZE,
    MSG_PARTIAL,
    MSG_POLISH,
    MSG_START,
    encode_wav,
    float_to_pcm16,
    to_b64,
)
from .base import Engine, EngineError, StreamSession

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..config import Config

_TOKEN_HINT = (
    "pass --token <secret> matching the server's `flow serve --token <secret>`, "
    "or set [engine] token in config.toml"
)


def _conn_hint(url: str) -> str:
    return f"is `flow serve` running at {url}? (flow serve --host 0.0.0.0 on the server machine)"


def _error_detail(r: Any) -> tuple[str, str]:
    """Pull (message, hint) out of a FastAPI error response body."""
    try:
        detail = r.json().get("detail")
    except ValueError:
        return str(getattr(r, "text", ""))[:200], ""
    if isinstance(detail, dict):
        return str(detail.get("message", detail)), str(detail.get("hint", ""))
    return str(detail), ""


def _ws_url(base: str, token: str) -> str:
    if base.startswith("https://"):
        url = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        url = "ws://" + base[len("http://") :]
    else:
        url = "ws://" + base
    url += "/v1/stream"
    if token:
        url += "?token=" + urllib.parse.quote(token, safe="")
    return url


class RemoteEngine(Engine):
    """HTTP(S) + websocket client for a remote `flow serve` instance."""

    def __init__(self, cfg: Config):
        url = (cfg.engine.server_url or "").strip()
        if not url:
            raise EngineError(
                "remote engine selected but no server URL is configured",
                hint="pass --server http://<host>:8765 or set [engine] server_url "
                "in config.toml",
            )
        if "://" not in url:
            url = "http://" + url
        self._base = url.rstrip("/")
        self._token = cfg.engine.token
        self._timeout = float(cfg.engine.request_timeout)
        self._cfg = cfg
        self._session = requests.Session()
        if self._token:
            self._session.headers["Authorization"] = f"Bearer {self._token}"

    def _language(self, language: str | None) -> str | None:
        return language if language is not None else self._cfg.whisper.language_or_none()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = f"{self._base}{path}"
        t = self._timeout if timeout is None else timeout
        try:
            r = self._session.request(method, url, json=json_body, timeout=t)
        except requests.Timeout as e:
            raise EngineError(
                f"flow server timed out after {t:.0f}s ({method} {url})",
                hint="the server may still be loading models; raise [engine] "
                "request_timeout if this keeps happening",
            ) from e
        except requests.RequestException as e:
            raise EngineError(
                f"cannot reach flow server: {e}", hint=_conn_hint(self._base)
            ) from e
        if r.status_code == 401:
            raise EngineError(
                "flow server rejected the request (HTTP 401 unauthorized)", hint=_TOKEN_HINT
            )
        if r.status_code >= 400:
            message, hint = _error_detail(r)
            raise EngineError(
                f"flow server returned HTTP {r.status_code} for {path}: {message}",
                hint=hint or _conn_hint(self._base),
            )
        try:
            data = r.json()
        except ValueError as e:
            raise EngineError(
                f"flow server sent a non-JSON response for {path}",
                hint=_conn_hint(self._base),
            ) from e
        return data if isinstance(data, dict) else {}

    # -- Engine API ---------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        language: str | None = None,
        vocab: str = "",
    ) -> str:
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return ""
        payload = {
            "audio_b64": to_b64(encode_wav(arr, sample_rate)),
            "language": self._language(language),
            "vocab": vocab,
        }
        data = self._request("POST", "/v1/transcribe", json_body=payload)
        return str(data.get("text", ""))

    def polish(self, text: str, *, language: str | None = None, vocab: str = "") -> str:
        if not text:
            return text
        payload = {"text": text, "language": self._language(language), "vocab": vocab}
        data = self._request("POST", "/v1/polish", json_body=payload)
        return str(data.get("text", "")) or text

    def polish_stream(
        self, text: str, *, language: str | None = None, vocab: str = ""
    ) -> Iterator[str]:
        # Single REST call: one full-text snapshot. On failure, yield the
        # input once (polish must never lose the user's words).
        try:
            yield self.polish(text, language=language, vocab=vocab)
        except EngineError as e:
            print(f"flow: remote polish failed: {e} ({e.hint})", file=sys.stderr)
            yield text

    def open_stream(
        self, sample_rate: int, *, language: str | None = None, vocab: str = ""
    ) -> StreamSession | None:
        return WsStreamSession(
            base_url=self._base,
            token=self._token,
            timeout=self._timeout,
            sample_rate=sample_rate,
            language=self._language(language),
            vocab=vocab,
        )

    def health(self) -> dict:
        return self._request("GET", "/v1/health", timeout=min(self._timeout, 10.0))

    def warm(self) -> None:
        try:
            self.health()
        except EngineError as e:
            print(f"flow: server not reachable yet: {e} — {e.hint}", file=sys.stderr)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


class WsStreamSession(StreamSession):
    """Live streaming over `/v1/stream` using websocket-client (sync).

    A reader thread collects server messages: `partial` frames land in a
    latest-value slot (deduplicated by `partial()`), everything else goes
    to a queue consumed by `finalize()`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float,
        sample_rate: int,
        language: str | None,
        vocab: str,
    ):
        try:
            import websocket
        except ImportError as e:  # pragma: no cover - dependency of the package
            raise EngineError(
                "websocket-client is not installed",
                hint="pip install websocket-client",
            ) from e
        self._base = base_url
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._latest: str | None = None
        self._last_returned: str | None = None
        self._q: queue.Queue[dict | None] = queue.Queue()
        self._closed = False
        self._dead_reason = ""
        url = _ws_url(base_url, token)
        try:
            self._ws = websocket.create_connection(url, timeout=self._timeout)
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status in (401, 403):
                raise EngineError(
                    "flow server rejected the stream connection (unauthorized)",
                    hint=_TOKEN_HINT,
                ) from e
            raise EngineError(
                f"cannot open a stream to the flow server: {e}",
                hint=_conn_hint(base_url),
            ) from e
        try:
            self._ws.send(
                json.dumps(
                    {
                        "type": MSG_START,
                        "sample_rate": int(sample_rate),
                        "language": language,
                        "vocab": vocab,
                    }
                )
            )
        except Exception as e:
            self.close()
            raise EngineError(
                f"stream handshake with flow server failed: {e}",
                hint=_conn_hint(base_url),
            ) from e
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="flow-ws-reader"
        )
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                frame = self._ws.recv()
                if frame is None or frame in ("", b""):
                    break  # connection closed by the server
                if isinstance(frame, (bytes, bytearray)):
                    continue  # server never sends binary; ignore
                try:
                    msg = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == MSG_PARTIAL:
                    with self._lock:
                        self._latest = str(msg.get("text", ""))
                else:
                    self._q.put(msg)
        except Exception as e:
            if not self._closed:
                self._dead_reason = str(e)
        finally:
            self._q.put(None)  # sentinel: no more messages

    # -- StreamSession API ----------------------------------------------------

    def feed(self, chunk: np.ndarray) -> None:
        if self._closed:
            return
        pcm = float_to_pcm16(chunk)
        if pcm.size == 0:
            return
        try:
            self._ws.send_binary(pcm.tobytes())
        except Exception as e:
            # Don't blow up the audio thread; finalize() will surface it.
            if not self._dead_reason:
                self._dead_reason = str(e)

    def partial(self) -> str | None:
        with self._lock:
            text = self._latest
            if text is not None and text != self._last_returned:
                self._last_returned = text
                return text
        return None

    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        try:
            self._ws.send(json.dumps({"type": MSG_FINALIZE, "vocab": vocab}))
        except Exception as e:
            raise EngineError(
                f"lost connection to flow server before finalize: {e}",
                hint=_conn_hint(self._base),
            ) from e
        yielded_any = False
        last = ""
        while True:
            try:
                msg = self._q.get(timeout=self._timeout)
            except queue.Empty:
                raise EngineError(
                    f"flow server did not finish within {self._timeout:.0f}s",
                    hint="the polish model may be cold-loading; raise [engine] "
                    "request_timeout if this keeps happening",
                ) from None
            if msg is None:
                detail = f" ({self._dead_reason})" if self._dead_reason else ""
                raise EngineError(
                    f"connection to flow server closed before the final result{detail}",
                    hint=_conn_hint(self._base),
                )
            mtype = msg.get("type")
            if mtype == MSG_POLISH:
                last = str(msg.get("text", ""))
                yielded_any = True
                yield last
            elif mtype == MSG_FINAL:
                text = str(msg.get("text", ""))
                if not yielded_any or text != last:
                    yield text
                self.close()
                return
            elif mtype == MSG_ERROR:
                raise EngineError(
                    str(msg.get("message", "flow server error")),
                    hint=str(msg.get("hint", "")) or _conn_hint(self._base),
                )

    def cancel(self) -> None:
        try:
            self._ws.send(json.dumps({"type": MSG_CANCEL}))
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
