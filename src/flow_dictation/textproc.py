"""Text post-processing shared across modules."""

from __future__ import annotations

import re


def sanitize(text: str) -> str:
    """Strip control and escape characters that could reprogram a terminal.

    Keeps newline and tab; drops everything else below 0x20 and DEL.
    Every string delivered to the focused window must pass through this.
    """
    return "".join(
        c for c in text if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 0x7F)
    )


def strip_quotes(s: str) -> str:
    """Remove a single pair of wrapping quotes the polisher sometimes adds."""
    if len(s) < 2:
        return s
    if s[0] == s[-1] and s[0] in ('"', "'") and s[0] not in s[1:-1]:
        return s[1:-1].strip()
    return s


def strip_think(s: str) -> str:
    """Remove complete <think>...</think> blocks.

    If a <think> tag is open without a closing tag, the model is still
    thinking: return "".
    """
    if "<think>" in s and "</think>" not in s:
        return ""
    if "<think>" in s:
        s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL)
    return s.strip()


def common_prefix_len(a: str, b: str) -> int:
    i = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            return i
        i += 1
    return i


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    """Case-insensitive whole-word replacements, applied after polish.

    Longer patterns are applied first so "vs code insiders" wins over
    "vs code". Replacement preserves the configured casing exactly.
    """
    if not replacements or not text:
        return text
    for pattern in sorted(replacements, key=len, reverse=True):
        replacement = replacements[pattern]
        if not pattern.strip():
            continue
        regex = re.compile(rf"(?<!\w){re.escape(pattern)}(?!\w)", re.IGNORECASE)
        text = regex.sub(lambda _m, r=replacement: r, text)
    return text
