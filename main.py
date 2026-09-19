"""Entry point for the packaged LanDrop executable.

Kept deliberately thin: all real logic lives in app/server.py (the
backend, fully tested against a live server + WebSocket in dev) and
app/tray.py (the Windows tray UI, not exercisable outside a real
Windows desktop -- see the note at the top of tray.py).
"""

from __future__ import annotations

import os
import time
import traceback
from pathlib import Path


def _startup_log(message: str) -> None:
    """Write early startup failures somewhere a packaged app can expose."""
    try:
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LanDrop"
        root.mkdir(parents=True, exist_ok=True)
        with (root / "landrop.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except Exception:
        # Logging must never prevent the application from starting.
        pass


_startup_log("process entry")

try:
    from app.tray import main
except BaseException:
    _startup_log("import failed:\n" + traceback.format_exc())
    raise

if __name__ == "__main__":
    try:
        _startup_log("calling tray main")
        main()
    except BaseException:
        _startup_log("tray main failed:\n" + traceback.format_exc())
        raise
