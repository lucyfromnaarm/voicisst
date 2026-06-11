# -*- mode: python -*-
"""PyInstaller spec for the standalone ``voicisst`` binary (onedir).

Build (with voicisst[local,server,ui] + pyinstaller installed):

    pyinstaller --noconfirm packaging/pyinstaller/voicisst.spec

Output lands in dist/voicisst/ — run dist/voicisst/voicisst.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

try:
    # SPECPATH (dir containing this spec) is injected by PyInstaller.
    spec_dir = SPECPATH  # noqa: F821
except NameError:  # pragma: no cover - direct execution outside PyInstaller
    spec_dir = os.path.dirname(os.path.abspath(__file__))


def _have(module: str) -> bool:
    """True when ``module`` is importable in the build environment."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


datas: list = []
binaries: list = []
hiddenimports: list = []

# faster-whisper + ctranslate2 ship model assets and native libraries that
# PyInstaller's static analysis misses; collect everything.
for package in ("faster_whisper", "ctranslate2"):
    if _have(package):
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden

# macOS/Windows sounddevice wheels bundle PortAudio inside the
# _sounddevice_data package (Linux wheels use the system libportaudio).
if _have("_sounddevice_data"):
    datas += collect_data_files("_sounddevice_data")

# The web UI (voicisst ui / voicisst run --ui) serves hand-written
# HTML/CSS/JS from src/voicisst/ui/static as package data; static analysis
# only follows imports, so collect the files explicitly.
if _have("voicisst.ui"):
    datas += collect_data_files("voicisst.ui")

# uvicorn picks its event loop / protocol implementations via importlib at
# runtime, invisible to static analysis.
if _have("uvicorn"):
    hiddenimports += collect_submodules("uvicorn")

# websocket-client (imported lazily by the remote engine).
if _have("websocket"):
    hiddenimports += ["websocket"]

# pynput resolves its platform backend dynamically; include only the
# backend(s) that can import on the build platform.
if _have("pynput"):
    if sys.platform == "darwin":
        hiddenimports += ["pynput.keyboard._darwin", "pynput.mouse._darwin"]
    elif sys.platform.startswith("win"):
        hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32"]
    else:
        hiddenimports += [
            "pynput.keyboard._xorg",
            "pynput.mouse._xorg",
            "pynput.keyboard._uinput",
            "pynput.mouse._uinput",
        ]

a = Analysis(
    [os.path.join(spec_dir, "entry.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="voicisst",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="voicisst",
)
