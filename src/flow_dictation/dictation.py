"""DictationApp: hotkey -> record -> transcribe -> polish -> deliver.

The orchestrator behind `flow run`, ported from the prototype's
run_loop/worker_loop pair: hotkey callbacks post events onto a queue, a
worker thread owns the recorder and the engine, and a small watchdog
ticker enforces the max-record cap and silence auto-stop — so both work
in toggle mode too, where no key release will ever arrive.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import audio as audio_mod
from . import clipboard
from .engine.base import EngineError
from .inject import windowinfo
from .notify import notify
from .streaming import StreamingTyper
from .textproc import apply_replacements, sanitize

if TYPE_CHECKING:
    import numpy as np

    from .config import Config
    from .engine.base import Engine, StreamSession
    from .hotkeys.base import HotkeyListener
    from .inject.base import Injector

# Events posted from the hotkey callbacks / watchdog to the worker thread.
EV_START = "start"
EV_STOP = "stop"
EV_CANCEL = "cancel"
EV_QUIT = "quit"

_PUMP_TICK_S = 0.05  # how often recorder chunks are fed to a stream session
_PROCESSING_SUFFIX = " [Processing…]"

ListenerFactory = Callable[..., "HotkeyListener"]
InjectorFactory = Callable[["Config"], "Injector"]


class DictationApp:
    """Blocking dictation loop: listen for the hotkey, speak, deliver text.

    `listener_factory` / `injector_factory` exist for dependency injection
    in tests; production code uses `hotkeys.get_listener` and
    `inject.get_injector`.
    """

    def __init__(
        self,
        cfg: Config,
        engine: Engine,
        injector: Injector | None = None,
        *,
        listener_factory: ListenerFactory | None = None,
        injector_factory: InjectorFactory | None = None,
        watchdog_tick_s: float = 1.0,
    ):
        self.cfg = cfg
        self.engine = engine
        self.injector = injector
        self._listener_factory = listener_factory
        self._injector_factory = injector_factory
        self._watchdog_tick_s = watchdog_tick_s

        self._queue: queue.Queue[str] = queue.Queue()
        self._shutdown = threading.Event()
        self._state_lock = threading.Lock()
        self._active = False  # a recording was requested and not yet stopped

        # Backspace pressed inside this window cancels the in-flight polish
        # (falls back to the raw transcript) — prototype's POLISHING /
        # CANCEL_POLISH choreography, scoped to the app instance.
        self._polishing = threading.Event()
        self._cancel_polish = threading.Event()

        self._listener: HotkeyListener | None = None
        self._recorder: Any = None
        self._worker: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._ticker_stop = threading.Event()

        # Per-utterance streaming state (owned by the worker thread).
        self._session: StreamSession | None = None
        self._typer: StreamingTyper | None = None
        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()
        self._vocab = ""
        self._silence: audio_mod.SilenceDetector | None = None
        self._silence_fed = 0

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        """Run dictation until stop() is called. Blocking."""
        cfg = self.cfg
        self._shutdown.clear()
        try:
            if self.injector is None:
                self.injector = self._build_injector()
            self._recorder = self._build_recorder()

            # Warm the engine in the background so the hotkey is live
            # immediately; the first utterance just waits a bit longer.
            threading.Thread(
                target=self._warm_engine, name="flow-warm", daemon=True
            ).start()

            self._worker = threading.Thread(
                target=self._worker_loop, name="flow-worker", daemon=True
            )
            self._worker.start()
            self._ticker_stop.clear()
            self._ticker = threading.Thread(
                target=self._watch_loop, name="flow-watchdog", daemon=True
            )
            self._ticker.start()

            listener_factory = self._listener_factory
            if listener_factory is None:
                from .hotkeys import get_listener

                listener_factory = get_listener
            self._listener = listener_factory(
                cfg, self._on_press, self._on_release, self._on_backspace
            )
            self._listener.start()

            mode = "hold" if cfg.hotkey.mode != "toggle" else "toggle"
            self._notify(
                "flow ready",
                f"{mode} mode — keys: {', '.join(cfg.hotkey.keys)}",
            )
            self._shutdown.wait()
        except KeyboardInterrupt:
            print("\nflow: shutting down", file=sys.stderr)
        finally:
            self._teardown()

    def stop(self) -> None:
        """Request a clean shutdown; run() unblocks and tears down."""
        self._shutdown.set()

    # -- construction helpers -------------------------------------------------

    def _build_injector(self) -> Injector:
        factory = self._injector_factory
        if factory is None:
            from .inject import get_injector

            factory = get_injector
        try:
            return factory(self.cfg)
        except EngineError:
            raise
        except Exception as e:
            # get_injector raises RuntimeError with the full fix hint baked in.
            raise EngineError(str(e)) from e

    def _build_recorder(self) -> Any:
        try:
            return audio_mod.Recorder(self.cfg.audio.sample_rate, self.cfg.audio.input_device)
        except Exception as e:
            raise EngineError(
                f"audio capture unavailable: {e}",
                hint="install PortAudio (Debian/Ubuntu: `sudo apt install libportaudio2`; "
                "macOS: `brew install portaudio`) and check `python -m sounddevice`",
            ) from e

    def _warm_engine(self) -> None:
        try:
            self.engine.warm()
        except EngineError as e:
            self._notify(f"engine warm-up failed: {e}", e.hint or "", "critical")
        except Exception as e:
            self._notify(f"engine warm-up failed: {e}", "", "critical")

    def _teardown(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as e:
                print(f"flow: listener stop failed: {e}", file=sys.stderr)
            self._listener = None
        self._request_stop(cancel=True)  # no-op unless a recording is live
        self._queue.put(EV_QUIT)
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None
        self._ticker_stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=2)
            self._ticker = None
        try:
            self.engine.close()
        except Exception as e:
            print(f"flow: engine close failed: {e}", file=sys.stderr)
        rec = self._recorder
        if rec is not None and rec.is_active():
            try:
                rec.stop()
            except Exception:
                pass

    # -- hotkey callbacks (any thread) ----------------------------------------

    def _on_press(self) -> None:
        if self.cfg.hotkey.mode == "toggle":
            with self._state_lock:
                active = self._active
            if active:
                self._request_stop()
            else:
                self._request_start()
        else:  # hold-to-talk
            self._request_start()

    def _on_release(self) -> None:
        if self.cfg.hotkey.mode != "toggle":
            self._request_stop()
        # toggle mode: release is ignored — press toggles.

    def _on_backspace(self) -> None:
        # Only honored inside the polish window so out-of-window Backspace
        # stays a normal edit key.
        if self._polishing.is_set():
            self._cancel_polish.set()

    def _request_start(self) -> None:
        with self._state_lock:
            if self._active:
                return
            self._active = True
        self._queue.put(EV_START)

    def _request_stop(self, *, cancel: bool = False) -> None:
        with self._state_lock:
            if not self._active:
                return
            self._active = False
        self._queue.put(EV_CANCEL if cancel else EV_STOP)

    # -- watchdog ticker --------------------------------------------------------

    def _watch_loop(self) -> None:
        """Max-record cap + silence auto-stop, checked on a 1s-ish tick.

        Runs independently of key events so both protections work in
        toggle/hands-free mode.
        """
        while not self._ticker_stop.wait(self._watchdog_tick_s):
            rec = self._recorder
            if rec is None or not rec.is_active():
                continue
            with self._state_lock:
                active = self._active
            if not active:
                continue
            cap_ms = self.cfg.audio.max_record_ms
            if rec.elapsed_ms() > cap_ms:
                self._notify("max record hit", f"{cap_ms / 1000:.0f}s cap — stopping", "normal")
                self._request_stop()
                continue
            det = self._silence
            if det is None:
                continue
            chunks = rec.chunks
            n = len(chunks)
            while self._silence_fed < n:
                det.feed(chunks[self._silence_fed])
                self._silence_fed += 1
            if det.triggered:
                self._log("silence auto-stop")
                self._request_stop()

    # -- worker thread ------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            ev = self._queue.get()
            if ev == EV_QUIT:
                self._cleanup_stream()
                return
            try:
                if ev == EV_START:
                    self._handle_start()
                elif ev in (EV_STOP, EV_CANCEL):
                    self._handle_stop(cancelled=(ev == EV_CANCEL))
            except Exception as e:
                self._notify("dictation error", str(e), "critical")
                self._beep("error")
                self._reset_after_error()

    def _handle_start(self) -> None:
        cfg = self.cfg
        self._silence = None
        self._vocab = self._collect_vocab()
        try:
            self._recorder.start()
        except Exception as e:
            with self._state_lock:
                self._active = False
            self._notify("recorder failed", str(e), "critical")
            self._beep("error")
            return
        if cfg.audio.auto_stop_silence_s > 0:
            self._silence_fed = 0
            self._silence = audio_mod.SilenceDetector(
                cfg.audio.auto_stop_silence_s, cfg.audio.rms_gate, cfg.audio.sample_rate
            )
        self._beep("start")
        self._log(f"listening (vocab: {self._vocab[:60]})" if self._vocab else "listening")

        if cfg.output.stream:
            session: StreamSession | None = None
            try:
                session = self.engine.open_stream(
                    cfg.audio.sample_rate, language=self._language(), vocab=self._vocab
                )
            except Exception as e:
                print(f"flow: open_stream failed ({e}); falling back to plain", file=sys.stderr)
            if session is not None:
                self._session = session
                self._typer = StreamingTyper(
                    session, self.injector, cfg.output.stream_tick_ms / 1000.0
                )
                self._pump_stop.clear()
                self._pump_thread = threading.Thread(
                    target=self._pump_loop,
                    args=(session, self._recorder),
                    name="flow-pump",
                    daemon=True,
                )
                self._pump_thread.start()
                self._typer.start()

    def _handle_stop(self, *, cancelled: bool) -> None:
        cfg = self.cfg
        session, self._session = self._session, None
        typer, self._typer = self._typer, None
        self._silence = None

        # Stop the live typer BEFORE the final pass so the injector isn't
        # driven from two threads at once (prototype lock discipline).
        streamed = ""
        if typer is not None:
            streamed = typer.stop()
        try:
            audio_arr, dur_ms = self._recorder.stop()
        except Exception as e:
            self._notify("recorder stop failed", str(e), "critical")
            if typer is not None:
                typer.erase_all()
            self._stop_pump()
            self._close_session(session, cancel=True)
            return
        # Recorder is stopped: chunks are final. Drain them into the stream
        # session before finalize so the server/local pass sees everything.
        self._stop_pump()

        if cancelled:
            self._beep("cancel")
            self._log("cancelled")
            if typer is not None:
                typer.erase_all()
            self._close_session(session, cancel=True)
            return
        self._beep("stop")

        def reject(reason: tuple[str, str] | None = None) -> None:
            # too-short / silent / muted — used exactly like the prototype.
            if typer is not None:
                typer.erase_all()
            self._close_session(session, cancel=True)
            if reason is not None:
                self._notify(reason[0], reason[1])

        if dur_ms < cfg.audio.min_record_ms:
            reject()
            return
        if audio_arr.size == 0:
            reject()
            return
        level = audio_mod.rms(audio_arr)
        if level < cfg.audio.muted_rms:
            reject(("mic muted?", f"rms={level:.6f} — check pavucontrol / pipewire"))
            return
        if level < cfg.audio.rms_gate:
            reject()
            return

        t0 = time.monotonic()
        if session is not None and typer is not None:
            raw, final, app_cls = self._finish_streaming(session, typer, streamed)
        else:
            raw, final, app_cls = self._finish_plain(audio_arr, dur_ms)
        elapsed = time.monotonic() - t0
        if not raw and not final:
            return
        if final and cfg.history.enabled:
            self._append_history(raw, final, app_cls)
        self._log(
            f"{dur_ms / 1000:.1f}s audio -> {len(final)} chars in {elapsed:.2f}s "
            f"(raw={raw[:60]!r})"
        )

    # -- plain (non-streaming) path ----------------------------------------------

    def _finish_plain(
        self, audio_arr: np.ndarray, dur_ms: float
    ) -> tuple[str, str, str | None]:
        cfg = self.cfg
        if cfg.audio.normalize:
            audio_arr = audio_mod.normalize(audio_arr)
        self._log(f"transcribing {dur_ms / 1000:.1f}s…")
        try:
            raw = str(
                self.engine.transcribe(
                    audio_arr,
                    cfg.audio.sample_rate,
                    language=self._language(),
                    vocab=self._vocab,
                )
            ).strip()
        except EngineError as e:
            self._notify(f"transcription failed: {e}", e.hint or "", "critical")
            self._beep("error")
            return "", "", None
        if not raw:
            self._log("empty transcript — nothing to deliver")
            return "", "", None
        cleaned = raw
        if cfg.polish.enabled:
            self._log(f"polishing… {raw[:80]!r}")
            try:
                cleaned = str(
                    self.engine.polish(raw, language=self._language(), vocab=self._vocab)
                ).strip() or raw
            except EngineError as e:
                # Engines shouldn't raise for polish-level errors, but if one
                # does, the user's words still win.
                self._notify(f"polish failed: {e}", e.hint or "")
                cleaned = raw
        final, app_cls = self._deliver(cleaned)
        return raw, final, app_cls

    # -- streaming path -------------------------------------------------------------

    def _finish_streaming(
        self, session: StreamSession, typer: StreamingTyper, streamed: str
    ) -> tuple[str, str, str | None]:
        cfg = self.cfg
        typer.set_suffix(_PROCESSING_SUFFIX)
        raw = streamed.strip()

        self._cancel_polish.clear()
        self._polishing.set()
        cleaned = ""
        cancelled = False
        gen = session.finalize(vocab=self._vocab)
        try:
            for snap in gen:
                if self._cancel_polish.is_set():
                    cancelled = True
                    break
                # Intermediate snapshots go to the screen raw (sanitized by
                # the typer); replacements are applied to the FINAL text only.
                cleaned = str(snap)
                typer.replace_with(cleaned)
        except EngineError as e:
            self._notify(f"processing failed: {e}", e.hint or "", "critical")
            self._beep("error")
        finally:
            try:
                gen.close()
            except Exception:
                pass
            self._polishing.clear()

        if cancelled:
            fallback = sanitize(raw or cleaned)
            typer.replace_with(fallback)
            self._notify("polish cancelled", "using raw transcript")
            final = fallback
        elif not cleaned.strip():
            typer.erase_all()
            self._close_session(session, cancel=True)
            self._log("empty transcript — nothing to deliver")
            return raw, "", None
        else:
            final = apply_replacements(sanitize(cleaned), cfg.replacements)
            typer.replace_with(final)
        self._close_session(session)
        app_cls = windowinfo.focused_window_class()
        return (raw or cleaned), final, app_cls

    def _pump_loop(self, session: StreamSession, recorder: Any) -> None:
        """Feed recorder chunks into the stream session as they arrive."""
        fed = 0
        while not self._pump_stop.wait(_PUMP_TICK_S):
            fed = self._feed_chunks(session, recorder, fed)
        # Final drain: runs after recorder.stop(), so every chunk is fed
        # before finalize() transcribes the full utterance.
        self._feed_chunks(session, recorder, fed)

    @staticmethod
    def _feed_chunks(session: StreamSession, recorder: Any, fed: int) -> int:
        chunks = recorder.chunks
        n = len(chunks)
        while fed < n:
            try:
                session.feed(chunks[fed])
            except Exception as e:
                print(f"flow: stream feed error: {e}", file=sys.stderr)
            fed += 1
        return fed

    def _stop_pump(self) -> None:
        thread, self._pump_thread = self._pump_thread, None
        if thread is not None:
            self._pump_stop.set()
            thread.join(timeout=2)

    # -- delivery -------------------------------------------------------------------

    def _deliver(self, text: str) -> tuple[str, str | None]:
        """Sanitize, apply replacements, then paste/type/copy per config.

        Returns (delivered text, focused window class) for the history log.
        """
        cfg = self.cfg
        final = apply_replacements(sanitize(text), cfg.replacements)
        if not final.strip():
            return "", None
        app_cls = windowinfo.focused_window_class()
        if windowinfo.looks_like_terminal(app_cls, cfg.output.terminal_classes):
            if clipboard.copy(final):
                self._notify(
                    "terminal detected",
                    f"({app_cls}) — text copied, press Ctrl+Shift+V to paste",
                    "normal",
                )
            else:
                self._notify(
                    "terminal detected; clipboard unavailable", str(app_cls), "critical"
                )
            return final, app_cls

        if cfg.output.mode == "paste":
            copied = clipboard.copy(final)
            if copied and self.injector.paste_chord():
                return final, app_cls
            if copied:
                self._notify("paste failed", "text is on clipboard — press Ctrl+V", "normal")
                return final, app_cls
            if self.injector.type_text(final):  # clipboard down: type instead
                return final, app_cls
        else:  # type mode
            if self.injector.type_text(final):
                return final, app_cls
            if clipboard.copy(final):
                self._notify(
                    "typing failed",
                    "text copied — press Ctrl+V "
                    "(Linux: `systemctl --user start ydotoold`)",
                    "normal",
                )
                return final, app_cls
        self._notify("delivery failed", "text printed to stderr", "critical")
        print(f"OUTPUT: {final}", file=sys.stderr)
        return final, app_cls

    # -- vocab / history -----------------------------------------------------------

    def _collect_vocab(self) -> str:
        """Spelling context: dictionary words + file terms + selection."""
        cfg = self.cfg
        terms: list[str] = []
        for word in cfg.dictionary.words:
            word = str(word).strip()
            if word:
                terms.append(word)
        path = cfg.dictionary.resolved_path()
        try:
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.split("#", 1)[0].strip()
                    if line:
                        terms.append(line)
        except OSError as e:
            print(f"flow: could not read dictionary {path}: {e}", file=sys.stderr)
        if cfg.dictionary.use_selection:
            selection = clipboard.read_primary_selection()
            if selection:
                terms.append(selection)
                if self.injector is not None:
                    # Deselect so the delivered text doesn't replace it.
                    self.injector.tap_escape()
        return ", ".join(terms)

    def _append_history(self, raw: str, text: str, app_cls: str | None) -> None:
        path = self.cfg.history.resolved_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "raw": raw,
                "text": text,
                "app": app_cls or "",
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"flow: could not write history {path}: {e}", file=sys.stderr)

    # -- small helpers ---------------------------------------------------------------

    def _language(self) -> str | None:
        return self.cfg.whisper.language_or_none()

    def _beep(self, kind: str) -> None:
        audio_mod.play_beep(kind, self.cfg.ui.beep)

    def _notify(self, summary: str, body: str = "", urgency: str = "low") -> None:
        notify(summary, body, urgency, enabled=self.cfg.ui.notify)

    @staticmethod
    def _log(message: str) -> None:
        print(f"flow: {message}", file=sys.stderr)

    @staticmethod
    def _close_session(session: StreamSession | None, *, cancel: bool = False) -> None:
        if session is None:
            return
        if cancel:
            try:
                session.cancel()
            except Exception:
                pass
        try:
            session.close()
        except Exception:
            pass

    def _cleanup_stream(self) -> None:
        typer, self._typer = self._typer, None
        session, self._session = self._session, None
        self._stop_pump()
        if typer is not None:
            try:
                typer.stop()
            except Exception:
                pass
        self._close_session(session, cancel=True)

    def _reset_after_error(self) -> None:
        with self._state_lock:
            self._active = False
        self._cleanup_stream()
        rec = self._recorder
        if rec is not None and rec.is_active():
            try:
                rec.stop()
            except Exception:
                pass
