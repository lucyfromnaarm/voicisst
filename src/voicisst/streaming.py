"""StreamingTyper: live-types the partial transcript while the user speaks.

Ported from the flow.py prototype, generalized over `StreamSession`
(partials come from the engine, local or remote) and `Injector` (key
events go through whichever backend is active).

Invariants preserved from the prototype:

- `last_typed` is always exactly the string we believe is on screen.
  Every backspace shrinks it, every type extends it, in lockstep with the
  key events actually sent — so we never backspace more than we typed,
  even after a partial failure.
- A single lock is held while reading a partial OR while emitting key
  events. The tick loop acquires it non-blocking and skips the tick if
  busy, so slow transcriptions never queue up.
- `stop()` joins the ticker then takes the lock once more, so any
  in-flight tick is finished and `last_typed` is settled before the
  caller reads it.
- Replacement is diff-aware: only the part after the common prefix is
  deleted/retyped, minimizing visible churn.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

from .textproc import common_prefix_len, sanitize

if TYPE_CHECKING:
    from .engine.base import StreamSession
    from .inject.base import Injector


class StreamingTyper:
    """Types live partials into the focused window, then lets the caller
    swap in polished text via `replace_with()`."""

    def __init__(self, session: StreamSession, injector: Injector, tick_s: float):
        self.session = session
        self.injector = injector
        self.tick_s = float(tick_s)
        self.last_typed = ""
        # Number of trailing chars of last_typed that are a "suffix" — a
        # temporary indicator (e.g. " [Processing…]") we can swap or clear
        # without touching the base text in front of it.
        self._suffix_len = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Held while reading a partial OR while emitting key events. A new
        # tick that finds the lock taken simply skips — prevents queueing.
        self._lock = threading.Lock()

    def start(self) -> None:
        self.last_typed = ""
        self._suffix_len = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        """Stop the tick loop; return whatever string we last typed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # Take the lock once more so any in-flight tick is finished and
        # last_typed is settled before the caller reads it.
        with self._lock:
            return self.last_typed

    def replace_with(self, target: str) -> None:
        """Atomically swap last_typed for `target` in the focused window.

        Deletes exactly the non-shared tail of last_typed, then types the
        new tail. last_typed is updated in lockstep so a partial failure
        leaves the counter consistent with what's on screen.
        """
        with self._lock:
            self._replace_locked(target)

    def erase_all(self) -> None:
        """Delete exactly the streamed text. Used on cancel / silence."""
        with self._lock:
            n = len(self.last_typed)
            if n and self.injector.backspace(n):
                self.last_typed = ""
                self._suffix_len = 0

    def set_suffix(self, suffix: str) -> None:
        """Swap the trailing status suffix in place. Base text (everything
        before the current suffix) is untouched. '' clears the suffix."""
        suffix = sanitize(suffix)
        with self._lock:
            old_n = self._suffix_len
            if old_n:
                if self.injector.backspace(old_n):
                    self.last_typed = self.last_typed[:-old_n]
                    self._suffix_len = 0
                else:
                    return
            if suffix:
                if self.injector.type_text(suffix):
                    self.last_typed += suffix
                    self._suffix_len = len(suffix)

    # -- internals ----------------------------------------------------------

    def _replace_locked(self, target: str) -> None:
        # Everything delivered to the focused window must be sanitized;
        # doing it here keeps the last_typed mirror exact.
        target = sanitize(target)
        # Reduce to common prefix to minimize visible churn.
        prefix = common_prefix_len(self.last_typed, target)
        to_delete = len(self.last_typed) - prefix
        if to_delete > 0:
            if self.injector.backspace(to_delete):
                self.last_typed = self.last_typed[:prefix]
            else:
                # Backspace failed — bail without touching last_typed.
                return
        tail = target[prefix:]
        if tail:
            if self.injector.type_text(tail):
                self.last_typed = self.last_typed + tail
        # Any replace_with-style operation invalidates suffix tracking.
        self._suffix_len = 0

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            if not self._lock.acquire(blocking=False):
                continue  # previous tick still running
            try:
                try:
                    raw = self.session.partial()
                except Exception as e:
                    print(f"stream partial error: {e}", file=sys.stderr)
                    continue
                if raw is None:  # unchanged / not ready
                    continue
                self._replace_locked(raw.strip())
            finally:
                self._lock.release()
