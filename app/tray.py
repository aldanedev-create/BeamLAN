"""LanDrop system tray application.

Runs the Flaxon server (HTTP + WebSocket) on a background thread, and
drives the Windows system tray icon + small popup dialogs on the main
thread. The tray app talks to its *own* server exactly the way a
future phone-browser client would (plain HTTP + WebSocket on
localhost) -- there's no special internal API, which keeps the surface
area small and means a browser-based companion client later can reuse
the exact same backend unchanged.

The tray shell keeps pystray for the icon and notifications. All
interactive file selection and transfer decisions are Jinax pages,
which can be opened locally or from a phone browser on the LAN.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import pystray
import uvicorn
import websockets
from PIL import Image, ImageDraw

if os.name == "nt":
    try:
        import webview
    except ImportError:  # pragma: no cover - only used by minimal source installs
        webview = None
else:  # pragma: no cover - the native shell is Windows-only
    webview = None

from .server import DOWNLOADS_DIR, create_app

HTTP_PORT = 53317  # arbitrary fixed default; falls back to an ephemeral
# port if already taken, see _pick_port().


def _startup_log(message: str) -> None:
    """Keep packaged startup failures visible even with a windowless build."""
    try:
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LanDrop"
        root.mkdir(parents=True, exist_ok=True)
        with (root / "landrop.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except Exception:
        # Startup diagnostics must never become a startup dependency.
        pass


def _pick_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _make_icon_image() -> Image.Image:
    """A simple placeholder glyph so the app has *something* in the tray
    without needing a designed asset for local development. Replace with
    a real icon (see packaging/Assets/) before shipping.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(37, 99, 235, 255))
    draw.polygon(
        [(size * 0.3, size * 0.45), (size * 0.55, size * 0.7), (size * 0.75, size * 0.3)],
        outline=(255, 255, 255, 255),
        width=4,
    )
    return img


class TrayApp:
    def __init__(self) -> None:
        _startup_log("TrayApp initializing")
        self.http_port = _pick_port(HTTP_PORT)
        _startup_log(f"selected port {self.http_port}")
        self.app = create_app(None, self.http_port)
        self.device_name = self.app.state.device_name
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server_thread: threading.Thread | None = None
        self.tray_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None
        self.window: Any | None = None
        self._opened_incoming: set[str] = set()

    # --- server + local event listener, run on a background thread ---

    def _run_server_thread(self) -> None:
        try:
            _startup_log("server thread starting")
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            config = uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=self.http_port,
                log_level="warning",
                # A windowed PyInstaller app has no stdout/stderr. Uvicorn's
                # default formatter probes those streams with isatty(), which
                # would stop the server thread before it binds its port.
                log_config=None,
            )
            server = uvicorn.Server(config)

            async def runner():
                listener_task = asyncio.create_task(self._listen_local_events())
                try:
                    await server.serve()
                finally:
                    listener_task.cancel()

            self.loop.run_until_complete(runner())
            _startup_log("server thread stopped")
        except BaseException:
            _startup_log("server thread failed:\n" + traceback.format_exc())

    async def _listen_local_events(self) -> None:
        url = f"ws://127.0.0.1:{self.http_port}/ws/local"
        while True:
            try:
                async with websockets.connect(url) as ws:
                    async for raw in ws:
                        self._handle_local_event(json.loads(raw))
            except (OSError, websockets.exceptions.WebSocketException):
                await asyncio.sleep(1)  # server still starting up; retry

    def _handle_local_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        transfer = event.get("transfer", {})

        if kind == "transfer.incoming":
            self._prompt_accept_decline(transfer)
        elif kind == "transfer.complete":
            self._notify(f"Received {transfer.get('filename')}")
        elif kind == "transfer.failed":
            self._notify(f"Transfer failed: {transfer.get('filename')}")

    # --- tray icon + menu, run on the main thread ---------------------

    def run(self) -> None:
        _startup_log("tray run starting")
        self.server_thread = threading.Thread(target=self._run_server_thread, daemon=True)
        self.server_thread.start()
        _startup_log("server thread launched")

        menu = pystray.Menu(
            pystray.MenuItem("Open LanDrop", self._on_open_ui),
            pystray.MenuItem("Send File...", self._on_send_file),
            pystray.MenuItem("Open Downloads Folder", self._on_open_downloads),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Running as {self.device_name}", None, enabled=False),
            pystray.MenuItem("Quit", self._on_quit),
        )
        self.icon = pystray.Icon("LanDrop", _make_icon_image(), "LanDrop", menu)
        # pywebview must own the process' main thread on Windows. Keep the
        # tray event loop alive in the background while the desktop window
        # displays the same Jinax app used by browser and phone clients.
        self.tray_thread = threading.Thread(
            target=self.icon.run,
            daemon=True,
            name="LanDropTray",
        )
        self.tray_thread.start()
        _startup_log("tray thread launched")

        if webview is None or os.name != "nt":
            _startup_log("native window unavailable; opening browser fallback")
            self._open_ui("/ui/")
            self.tray_thread.join()
            return

        self._wait_for_server()
        try:
            self.window = webview.create_window(
                "LanDrop",
                f"http://127.0.0.1:{self.http_port}/ui/",
                width=1120,
                height=760,
                min_size=(760, 560),
                resizable=True,
            )
            _startup_log("native desktop window starting")
            webview.start(debug=False)
            _startup_log("native desktop window stopped")
        except BaseException:
            # WebView2 is a Windows component and may be absent on a fresh
            # machine. Keep the app usable through the same local UI.
            _startup_log("native desktop window failed; opening browser fallback:\n" + traceback.format_exc())
            self._open_ui("/ui/")

    def _wait_for_server(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.http_port), timeout=0.25):
                    _startup_log("server is accepting local connections")
                    return
            except OSError:
                time.sleep(0.1)
        _startup_log("server readiness timeout; opening native window anyway")

    def _notify(self, message: str) -> None:
        if self.icon is not None:
            self.icon.notify(message, title="LanDrop")

    def _on_open_downloads(self, icon, item) -> None:
        downloads = self.app.state.downloads_dir if self.app.state.custom_download_dir else DOWNLOADS_DIR
        downloads.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(downloads)  # type: ignore[attr-defined]
        else:
            webbrowser.open(downloads.as_uri())

    def _open_ui(self, path: str = "/") -> None:
        webbrowser.open(f"http://127.0.0.1:{self.http_port}{path}")

    def _on_open_ui(self, icon, item) -> None:
        self._show_window("/ui/")

    def _on_quit(self, icon, item) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                _startup_log("native window close failed:\n" + traceback.format_exc())
        icon.stop()

    # --- send flow: pick file, pick peer, offer, wait for accept, upload

    def _on_send_file(self, icon, item) -> None:
        self._show_window("/ui/send")

    def _show_window(self, path: str) -> None:
        if self.window is not None:
            try:
                self.window.load_url(f"http://127.0.0.1:{self.http_port}{path}")
                self.window.show()
                return
            except Exception:
                _startup_log("native window could not be shown:\n" + traceback.format_exc())
        self._open_ui(path)

    async def _send_file_to_peer(self, path: Path, peer: dict) -> None:
        size = path.stat().st_size
        base_url = f"http://{peer['address']}:{peer['http_port']}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{base_url}/api/transfers",
                json={
                    "filename": path.name,
                    "size": size,
                    "sender_name": self.device_name,
                    "sender_device_id": self.app.state.discovery.device_id,
                },
            )
            r.raise_for_status()
            transfer_id = r.json()["transfer_id"]

            status = await self._wait_for_decision(peer, transfer_id)
            if status != "accepted":
                self._notify(f"{peer['name']} declined the file")
                return

            data = path.read_bytes()
            upload = await client.post(
                f"{base_url}/api/transfers/{transfer_id}/upload",
                content=data,
                timeout=120.0,
            )
            upload.raise_for_status()
            self._notify(f"Sent {path.name} to {peer['name']}")

    async def _wait_for_decision(self, peer: dict, transfer_id: str) -> str:
        url = f"ws://{peer['address']}:{peer['http_port']}/ws/transfers/{transfer_id}"
        async with websockets.connect(url) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            event = json.loads(raw)
            return event.get("transfer", {}).get("status", "declined")

    def _prompt_accept_decline(self, transfer: dict) -> None:
        transfer_id = transfer.get("transfer_id")
        if not transfer_id or transfer_id in self._opened_incoming:
            return
        self._opened_incoming.add(transfer_id)
        self._open_ui(f"/ui/incoming/{transfer_id}")

    async def _respond_to_transfer(self, transfer_id: str, endpoint: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{self.http_port}/api/transfers/{transfer_id}/{endpoint}"
            )


def main() -> None:
    _startup_log("tray main entered")
    TrayApp().run()


if __name__ == "__main__":
    main()
