"""Shared fixtures. Tests run fully headless: no audio, network, GPU, DISPLAY."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `from helpers import FakeEngine` work regardless of invocation dir.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from voicisst.config import Config, load_config  # noqa: E402


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Pure-default Config: no config file, no environment leakage."""
    return load_config(path=tmp_path / "missing.toml", env={})
