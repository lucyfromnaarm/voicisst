"""Tests for StreamingTyper — fake session + recording fake injector."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from voicisst.config import OutputConfig
from voicisst.engine.base import StreamSession
from voicisst.inject.base import Injector
from voicisst.streaming import StreamingTyper


class FakeInjector(Injector):
    """Records (op, arg) tuples; failures switchable per-op."""

    name = "fake"

    def __init__(self):
        super().__init__(OutputConfig())
        self.ops: list[tuple[str, object]] = []
        self.fail_backspace = False
        self.fail_type = False

    @classmethod
    def available(cls) -> bool:
        return True

    def type_text(self, text: str) -> bool:
        if self.fail_type:
            return False
        self.ops.append(("type", text))
        return True

    def backspace(self, n: int) -> bool:
        if self.fail_backspace:
            return False
        self.ops.append(("bs", n))
        return True

    def paste_chord(self) -> bool:
        return True

    def tap_escape(self) -> bool:
        return True

    @property
    def screen(self) -> str:
        """Reconstruct what would be on screen from the recorded ops."""
        s = ""
        for op, arg in self.ops:
            if op == "type":
                s += arg  # type: ignore[operator]
            else:
                assert isinstance(arg, int) and arg <= len(s), "over-backspace!"
                s = s[: len(s) - arg]
        return s


class ScriptedSession(StreamSession):
    """partial() pops scripted items: str, None, or an Exception to raise."""

    def __init__(self, items: list):
        self.items = list(items)

    def feed(self, chunk) -> None:
        pass

    def partial(self) -> str | None:
        if not self.items:
            return None
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def finalize(self, *, vocab: str = "") -> Iterator[str]:
        yield ""

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class EndlessSession(ScriptedSession):
    """Always returns a longer string — used to prove stop() settles."""

    def __init__(self):
        super().__init__([])
        self.i = 0

    def partial(self) -> str | None:
        self.i += 1
        return "x" * self.i


def make_typer(items=(), tick_s: float = 0.01) -> tuple[StreamingTyper, FakeInjector]:
    inj = FakeInjector()
    return StreamingTyper(ScriptedSession(list(items)), inj, tick_s), inj


def wait_until(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


# -- replace_with -------------------------------------------------------------


def test_replace_with_diffs_on_common_prefix():
    typer, inj = make_typer()
    typer.replace_with("hello world")
    typer.replace_with("hello there")
    assert inj.ops == [("type", "hello world"), ("bs", 5), ("type", "there")]
    assert typer.last_typed == "hello there"
    assert inj.screen == "hello there"


def test_replace_with_identical_target_is_noop():
    typer, inj = make_typer()
    typer.replace_with("same")
    typer.replace_with("same")
    assert inj.ops == [("type", "same")]


def test_replace_with_shrinking_target():
    typer, inj = make_typer()
    typer.replace_with("hello")
    typer.replace_with("he")
    assert inj.ops == [("type", "hello"), ("bs", 3)]
    assert typer.last_typed == "he"


def test_replace_with_empty_erases():
    typer, inj = make_typer()
    typer.replace_with("abc")
    typer.replace_with("")
    assert inj.ops == [("type", "abc"), ("bs", 3)]
    assert typer.last_typed == ""


def test_backspace_failure_never_corrupts_mirror():
    typer, inj = make_typer()
    typer.replace_with("abc")
    inj.fail_backspace = True
    typer.replace_with("x")  # needs backspaces -> fails -> bail untouched
    assert typer.last_typed == "abc"
    assert inj.ops == [("type", "abc")]
    inj.fail_backspace = False
    typer.replace_with("x")
    assert typer.last_typed == "x"
    assert inj.screen == "x"


def test_type_failure_keeps_mirror_consistent():
    typer, inj = make_typer()
    typer.replace_with("ab")
    inj.fail_type = True
    typer.replace_with("abcd")  # nothing deleted, type fails
    assert typer.last_typed == "ab"  # mirror still matches the screen
    assert inj.screen == "ab"


def test_replace_with_sanitizes_target():
    typer, inj = make_typer()
    typer.replace_with("a\x07b\x1bc")
    assert inj.ops == [("type", "abc")]
    assert typer.last_typed == "abc"


# -- suffix -------------------------------------------------------------------


def test_suffix_set_swap_clear():
    typer, inj = make_typer()
    typer.replace_with("hi")
    typer.set_suffix(" [1]")
    typer.set_suffix(" [22]")
    typer.set_suffix("")
    assert inj.ops == [
        ("type", "hi"),
        ("type", " [1]"),
        ("bs", 4),
        ("type", " [22]"),
        ("bs", 5),
    ]
    assert typer.last_typed == "hi"
    assert inj.screen == "hi"


def test_suffix_backspace_failure_keeps_old_suffix():
    typer, inj = make_typer()
    typer.replace_with("hi")
    typer.set_suffix("…")
    inj.fail_backspace = True
    typer.set_suffix("!!")  # cannot remove old suffix -> bail
    assert typer.last_typed == "hi…"
    inj.fail_backspace = False
    typer.set_suffix("!!")
    assert typer.last_typed == "hi!!"
    assert inj.screen == "hi!!"


def test_replace_with_invalidates_suffix_tracking():
    typer, inj = make_typer()
    typer.replace_with("hi")
    typer.set_suffix("…")
    typer.replace_with("hi…")  # identical text, but suffix is now "owned"
    inj.ops.clear()
    typer.set_suffix("?")  # must NOT backspace the old suffix
    assert inj.ops == [("type", "?")]
    assert typer.last_typed == "hi…?"


# -- erase_all ---------------------------------------------------------------


def test_erase_all_deletes_exactly_what_was_typed():
    typer, inj = make_typer()
    typer.replace_with("abcd")
    typer.set_suffix("!")
    typer.erase_all()
    assert inj.ops[-1] == ("bs", 5)
    assert typer.last_typed == ""
    assert inj.screen == ""


def test_erase_all_noop_when_empty():
    typer, inj = make_typer()
    typer.erase_all()
    assert inj.ops == []


# -- tick loop ----------------------------------------------------------------


def test_tick_loop_types_partials_with_diff():
    typer, inj = make_typer(items=["hel", None, "hello "])
    typer.start()
    try:
        assert wait_until(lambda: typer.last_typed == "hello")
    finally:
        final = typer.stop()
    assert final == "hello"  # trailing whitespace stripped before typing
    assert inj.ops == [("type", "hel"), ("type", "lo")]
    assert inj.screen == "hello"


def test_tick_loop_tolerates_partial_exception(capsys):
    typer, inj = make_typer(items=[RuntimeError("transcriber hiccup"), "ok"])
    typer.start()
    try:
        assert wait_until(lambda: typer.last_typed == "ok")
    finally:
        final = typer.stop()
    assert final == "ok"
    assert "transcriber hiccup" in capsys.readouterr().err


def test_stop_settles_and_returns_last_typed():
    inj = FakeInjector()
    typer = StreamingTyper(EndlessSession(), inj, tick_s=0.01)
    typer.start()
    assert wait_until(lambda: len(typer.last_typed) >= 2)
    final = typer.stop()
    assert final == typer.last_typed
    ops_after_stop = len(inj.ops)
    time.sleep(0.05)  # the ticker must really be gone
    assert len(inj.ops) == ops_after_stop
    assert inj.screen == final


def test_start_resets_state():
    typer, inj = make_typer()
    typer.replace_with("leftover")
    typer.set_suffix("!")
    typer.start()
    try:
        assert typer.last_typed == ""
    finally:
        assert typer.stop() == ""
    inj.ops.clear()
    typer.set_suffix("?")  # old suffix tracking must be gone
    assert inj.ops == [("type", "?")]


@pytest.mark.parametrize("partial_value", [None, ""])
def test_tick_loop_skips_none_but_honors_empty(partial_value):
    typer, inj = make_typer(items=["abc", partial_value])
    typer.start()
    try:
        assert wait_until(lambda: typer.last_typed == "abc")
        if partial_value is None:
            time.sleep(0.05)
            assert typer.last_typed == "abc"  # None = unchanged, keep text
        else:
            assert wait_until(lambda: typer.last_typed == "")  # "" erases
    finally:
        typer.stop()
