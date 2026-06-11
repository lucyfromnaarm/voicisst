"""State event bus: dictation state for the tray icon and the web UI.

DictationApp publishes; the tray and the web dashboard subscribe. Kept
deliberately tiny and dependency-free — this must never be able to break
dictation itself.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Canonical states, in rough lifecycle order.
IDLE = "idle"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
POLISHING = "polishing"
DELIVERING = "delivering"
ERROR = "error"
STOPPED = "stopped"

ALL_STATES = (IDLE, LISTENING, TRANSCRIBING, POLISHING, DELIVERING, ERROR, STOPPED)


@dataclass(frozen=True)
class StateEvent:
    state: str
    detail: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"state": self.state, "detail": self.detail, "ts": self.ts}


class StateBus:
    """Thread-safe publish/subscribe for StateEvents.

    Subscriber callbacks run on the publisher's thread and must be quick;
    anything slow should hand off to its own queue/loop. Subscriber
    exceptions are swallowed (logged to stderr) — a broken UI must never
    take down dictation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Callable[[StateEvent], None]] = {}
        self._next_id = 1
        self._last = StateEvent(IDLE)

    @property
    def last(self) -> StateEvent:
        with self._lock:
            return self._last

    def subscribe(self, fn: Callable[[StateEvent], None]) -> int:
        """Register a callback; it is immediately invoked with the latest
        event so late joiners (the web UI) start in sync. Returns an id
        for unsubscribe()."""
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subs[sub_id] = fn
            last = self._last
        self._safe_call(fn, last)
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def publish(self, state: str, detail: str = "") -> StateEvent:
        event = StateEvent(state, detail)
        with self._lock:
            self._last = event
            subs = list(self._subs.values())
        for fn in subs:
            self._safe_call(fn, event)
        return event

    @staticmethod
    def _safe_call(fn: Callable[[StateEvent], None], event: StateEvent) -> None:
        try:
            fn(event)
        except Exception as e:  # noqa: BLE001 — isolation is the whole point
            print(f"voicisst events: subscriber error ignored: {e}", file=sys.stderr)
