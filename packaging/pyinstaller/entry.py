"""PyInstaller entry point for the standalone ``voicisst`` binary.

PyInstaller's Analysis wants a real script file rather than a console-script
shim, so this thin wrapper exists. All logic lives in ``voicisst.cli``.
"""

from __future__ import annotations

import multiprocessing
import sys

from voicisst.cli import main

if __name__ == "__main__":
    # Required in frozen apps: ctranslate2/faster-whisper may spawn worker
    # processes, which would otherwise re-run the whole app on Windows/macOS.
    multiprocessing.freeze_support()
    sys.exit(main())
