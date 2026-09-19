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
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import pystray
import uvicorn
import websockets
from PIL import Image, ImageDraw

from .server import DOWNLOADS_DIR, create_app

HTTP_PORT = 53317  # arbitrary fixed default; falls back to an ephemeral
# port if already taken, see _pick_port().


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
        self.http_port = _pick_port(HTTP_PORT)
        self.app = create_app(None, self.http_port)
        self.device_name = self.app.state.device_name
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None
        self._opened_incoming: set[str] = set()

    # --- server + local event listener, run on a background thread ---

    def _run_server_thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.http_port, log_level="warning"
        )
        server = uvicorn.Server(config)

        async def runner():
            listener_task = asyncio.create_task(self._listen_local_events())
            try:
                await server.serve()
            finally:
                listener_task.cancel()

        self.loop.run_until_complete(runner())

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
        self.server_thread = threading.Thread(target=self._run_server_thread, daemon=True)
        self.server_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem("Open LanDrop", self._on_open_ui),
            pystray.MenuItem("Send File...", self._on_send_file),
            pystray.MenuItem("Open Downloads Folder", self._on_open_downloads),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Running as {self.device_name}", None, enabled=False),
            pystray.MenuItem("Quit", self._on_quit),
        )
        self.icon = pystray.Icon("LanDrop", _make_icon_image(), "LanDrop", menu)
        self.icon.run()

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
        self._open_ui("/")

    def _on_quit(self, icon, item) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        icon.stop()

    # --- send flow: pick file, pick peer, offer, wait for accept, upload

    def _on_send_file(self, icon, item) -> None:
        self._open_ui("/ui/send")

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
    TrayApp().run()


if __name__ == "__main__":
    main()
