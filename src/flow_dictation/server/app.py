"""FastAPI app factory + uvicorn entry point for `flow serve`.

The engine is injected so tests can pass a fake. All blocking engine calls
run in a threadpool — the event loop stays responsive while Whisper works.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import __version__
from ..engine.base import Engine, EngineError
from ..protocol import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SAMPLES,
    MSG_CANCEL,
    MSG_ERROR,
    MSG_FINAL,
    MSG_FINALIZE,
    MSG_PARTIAL,
    MSG_POLISH,
    MSG_START,
    decode_wav,
    from_b64,
    pcm16_to_float,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..config import Config

# Launch a streaming partial pass once at least this much new audio arrived.
_PARTIAL_STEP_S = 0.5
_DONE = object()  # sentinel for draining sync generators via next()


def create_app(engine: Engine, *, token: str = "") -> FastAPI:
    """Build the flow server app around an injected Engine."""
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.concurrency import run_in_threadpool
    except ImportError as e:
        raise EngineError(
            "fastapi is not installed",
            hint="install the server extra: pip install 'flow-dictation[server]'",
        ) from e

    # FastAPI resolves endpoint annotations through *module* globals; these
    # names are imported lazily (hard rule), so publish them for
    # `from __future__ import annotations` to keep working.
    globals()["Request"] = Request
    globals()["WebSocket"] = WebSocket

    app = FastAPI(title="flow-dictation", version=__version__)

    # -- helpers ------------------------------------------------------------

    def _token_ok(supplied: str) -> bool:
        # Constant-time comparison: never leak the token via timing.
        return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

    def _require_auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not (supplied.startswith(prefix) and _token_ok(supplied[len(prefix) :])):
            raise HTTPException(
                status_code=401,
                detail={
                    "message": "unauthorized",
                    "hint": "send 'Authorization: Bearer <token>' matching the "
                    "server's `flow serve --token <token>`",
                },
            )

    def _payload_too_large(n_bytes: int, what: str) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail={
                "message": f"{what} is {n_bytes} bytes, over the flow server's "
                f"{MAX_AUDIO_BYTES}-byte audio cap (~120 s of audio)",
                "hint": "send shorter utterances, or split long recordings "
                "into multiple /v1/transcribe requests",
            },
        )

    async def _json_body(request: Request) -> dict[str, Any]:
        # Reject oversized bodies before reading them into memory. The limit
        # is the audio cap plus base64 (4/3) expansion and JSON envelope room.
        body_cap = MAX_AUDIO_BYTES * 4 // 3 + 65536
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > body_cap:
            raise _payload_too_large(int(content_length), "request body")
        try:
            data = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"request body must be JSON: {e}"
            ) from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        return data

    def _decode_audio(data: dict[str, Any]) -> tuple[np.ndarray, int]:
        b64 = data.get("audio_b64")
        if not isinstance(b64, str) or not b64:
            raise HTTPException(
                status_code=400,
                detail="missing 'audio_b64' — base64 of a 16-bit PCM mono WAV "
                "(see protocol.encode_wav)",
            )
        if len(b64) > MAX_AUDIO_BYTES * 4 // 3 + 4:  # cap before decoding
            raise _payload_too_large(len(b64) * 3 // 4, "decoded audio payload")
        try:
            wav = from_b64(b64)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if len(wav) > MAX_AUDIO_BYTES:
            raise _payload_too_large(len(wav), "decoded audio payload")
        try:
            return decode_wav(wav)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _engine_http_error(e: EngineError) -> HTTPException:
        return HTTPException(status_code=500, detail={"message": str(e), "hint": e.hint})

    def _opts(data: dict[str, Any]) -> tuple[str | None, str]:
        language = data.get("language") or None
        vocab = str(data.get("vocab") or "")
        return (str(language) if language is not None else None), vocab

    # -- REST ---------------------------------------------------------------

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        _require_auth(request)
        try:
            info = await run_in_threadpool(engine.health)
        except EngineError as e:
            raise HTTPException(
                status_code=503, detail={"message": str(e), "hint": e.hint}
            ) from e
        out: dict[str, Any] = {"status": "ok", "version": __version__}
        out.update(info)
        return out

    @app.post("/v1/transcribe")
    async def transcribe(request: Request) -> dict[str, Any]:
        _require_auth(request)
        data = await _json_body(request)
        audio, sample_rate = _decode_audio(data)
        language, vocab = _opts(data)
        try:
            text = await run_in_threadpool(
                engine.transcribe, audio, sample_rate, language=language, vocab=vocab
            )
        except EngineError as e:
            raise _engine_http_error(e) from e
        return {"text": text}

    @app.post("/v1/polish")
    async def polish(request: Request) -> dict[str, Any]:
        _require_auth(request)
        data = await _json_body(request)
        text = data.get("text")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="missing 'text' (string)")
        language, vocab = _opts(data)
        try:
            polished = await run_in_threadpool(
                engine.polish, text, language=language, vocab=vocab
            )
        except EngineError as e:
            raise _engine_http_error(e) from e
        return {"text": polished}

    @app.post("/v1/process")
    async def process(request: Request) -> dict[str, Any]:
        _require_auth(request)
        data = await _json_body(request)
        audio, sample_rate = _decode_audio(data)
        language, vocab = _opts(data)
        do_polish = bool(data.get("polish", True))
        try:
            raw = await run_in_threadpool(
                engine.transcribe, audio, sample_rate, language=language, vocab=vocab
            )
            text = raw
            if do_polish and raw:
                text = await run_in_threadpool(
                    engine.polish, raw, language=language, vocab=vocab
                )
        except EngineError as e:
            raise _engine_http_error(e) from e
        return {"raw": raw, "text": text}

    # -- websocket streaming --------------------------------------------------

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        # Prefer the Authorization header (it stays out of access logs);
        # ?token= is still accepted for older clients.
        auth = ws.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer ") :]
        else:
            supplied = ws.query_params.get("token", "")
        if token and not _token_ok(supplied):
            # Deny the upgrade before accept(): uvicorn turns this into an
            # HTTP 403 handshake rejection, so clients fail at connect time
            # instead of after dictating a whole utterance.
            with suppress(Exception):
                await ws.close(code=4401)
            return
        await ws.accept()

        sample_rate = 16000
        language: str | None = None
        vocab = ""
        chunks: list[np.ndarray] = []
        total = 0
        passed = 0  # samples already covered by the last partial launch
        last_partial = ""
        task: asyncio.Task | None = None
        finalize_vocab = ""
        cancelled = False

        async def _partial_pass(audio: np.ndarray) -> None:
            nonlocal last_partial
            try:
                text = (
                    await run_in_threadpool(
                        engine.transcribe, audio, sample_rate,
                        language=language, vocab=vocab,
                    )
                ).strip()
            except Exception as e:
                print(f"flow serve: partial transcribe error: {e}", file=sys.stderr)
                return
            if text and text != last_partial:
                last_partial = text
                with suppress(Exception):
                    await ws.send_json({"type": MSG_PARTIAL, "text": text})

        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    cancelled = True
                    break
                data_bytes = msg.get("bytes")
                if data_bytes is not None:
                    pcm = pcm16_to_float(data_bytes)
                    if pcm.size:
                        chunks.append(pcm)
                        total += int(pcm.size)
                    if total > MAX_AUDIO_SAMPLES:
                        if task is not None:
                            task.cancel()
                        with suppress(Exception):
                            await ws.send_json(
                                {
                                    "type": MSG_ERROR,
                                    "message": "stream exceeded the audio cap "
                                    f"({total} samples > {MAX_AUDIO_SAMPLES}, "
                                    "~120 s of audio)",
                                    "hint": "finalize or cancel sooner — flow caps "
                                    "one streamed utterance to bound server memory",
                                }
                            )
                            await ws.close(code=1009)  # message too big
                        return
                    if (task is None or task.done()) and (
                        total - passed >= int(_PARTIAL_STEP_S * sample_rate)
                    ):
                        passed = total
                        buf = np.concatenate(chunks).astype(np.float32)
                        task = asyncio.create_task(_partial_pass(buf))
                    continue
                text_frame = msg.get("text")
                if text_frame is None:
                    continue
                try:
                    data = json.loads(text_frame)
                except json.JSONDecodeError:
                    with suppress(Exception):
                        await ws.send_json(
                            {
                                "type": MSG_ERROR,
                                "message": "bad JSON text frame",
                                "hint": "frames must be JSON objects with a 'type' field",
                            }
                        )
                    continue
                mtype = data.get("type") if isinstance(data, dict) else None
                if mtype == MSG_START:
                    sample_rate = int(data.get("sample_rate") or 16000)
                    lang = data.get("language") or None
                    language = str(lang) if lang is not None else None
                    vocab = str(data.get("vocab") or "")
                elif mtype == MSG_FINALIZE:
                    finalize_vocab = str(data.get("vocab") or "") or vocab
                    break
                elif mtype == MSG_CANCEL:
                    cancelled = True
                    break

            if task is not None:
                with suppress(Exception):
                    await task
            if cancelled:
                with suppress(Exception):
                    await ws.close()
                return

            # finalize: full-buffer transcribe with vocab, then stream polish.
            audio = (
                np.concatenate(chunks).astype(np.float32)
                if chunks
                else np.zeros(0, dtype=np.float32)
            )
            raw = ""
            if audio.size:
                raw = (
                    await run_in_threadpool(
                        engine.transcribe, audio, sample_rate,
                        language=language, vocab=finalize_vocab,
                    )
                ).strip()
            last = raw
            if raw:
                gen = engine.polish_stream(raw, language=language, vocab=finalize_vocab)
                try:
                    while True:
                        snap = await run_in_threadpool(next, gen, _DONE)
                        if snap is _DONE:
                            break
                        last = str(snap)
                        await ws.send_json({"type": MSG_POLISH, "text": last})
                finally:
                    gen.close()
            await ws.send_json({"type": MSG_FINAL, "text": last, "raw": raw})
            with suppress(Exception):
                await ws.close()
        except Exception as e:
            if task is not None:
                task.cancel()
            print(f"flow serve: stream error: {e}", file=sys.stderr)
            with suppress(Exception):
                await ws.send_json(
                    {
                        "type": MSG_ERROR,
                        "message": str(e),
                        "hint": getattr(e, "hint", ""),
                    }
                )
                await ws.close(code=1011)

    return app


def serve(cfg: Config) -> None:
    """`flow serve` entry point: warm a LocalEngine, run uvicorn (blocking)."""
    try:
        import uvicorn
    except ImportError as e:
        raise EngineError(
            "uvicorn is not installed",
            hint="install the server extra: pip install 'flow-dictation[server]'",
        ) from e
    from ..engine.local import LocalEngine

    engine = LocalEngine(cfg)
    print("flow serve: warming models (first load can take a while)…", file=sys.stderr)
    engine.warm()

    host = cfg.server.host
    port = cfg.server.port
    token = cfg.server.token
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print(
            "\n"
            "*** WARNING: flow serve is binding to a non-loopback address "
            f"({host}) WITHOUT a token. ***\n"
            "*** Anyone on the network can transcribe audio on this machine "
            "and read the results. ***\n"
            "*** Set one with `flow serve --token <secret>` (clients pass the "
            "same via --token). ***\n",
            file=sys.stderr,
        )

    app = create_app(engine, token=token)
    uvicorn.run(app, host=host, port=port, log_level="info")
