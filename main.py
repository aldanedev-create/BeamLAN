"""Entry point for the packaged LanDrop executable.

Kept deliberately thin: all real logic lives in app/server.py (the
backend, fully tested against a live server + WebSocket in dev) and
app/tray.py (the Windows tray UI, not exercisable outside a real
Windows desktop -- see the note at the top of tray.py).
"""

from app.tray import main

if __name__ == "__main__":
    main()
