"""LanDrop backend.

This is the entire "server" -- it runs locally on the user's own machine
(no cloud, no account) and does three jobs:

1. Expose the peers this device has discovered on the LAN (via
   discovery.DiscoveryService) over HTTP, so the tray UI can show a
   picker.
2. Handle the transfer lifecycle (offer -> accept/decline -> upload ->
   complete) between two LanDrop instances talking directly to each
   other over the LAN.
3. Push live events to the local tray UI (peer list changes, incoming
   transfer requests, transfer progress) over a local WebSocket, and
   push per-transfer status to the *other* device's tray UI over a
   second WebSocket scoped to that one transfer -- so the sender finds
   out immediately when the receiver accepts/declines, without polling.

Route parameter syntax note: Flaxon uses Flask-style <param> path
parameters, not FastAPI-style {param} -- the latter silently 404s
instead of erroring, so it's worth getting right the first time.
"""

from __future__ import annotations

import time
import uuid
import asyncio
import base64
import io
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import qrcode
import websockets
from flaxon import Flaxon
from flaxon.http import JSONResponse, Response
from flaxon.jinax import Jinax

from .config import SettingsStore
from .discovery import DiscoveryService
from .modules.ui import ui

MAX_TRANSFER_BYTES = 200 * 1024 * 1024  # 200MB cap for the v1 buffered upload path
BROWSER_SHARE_TTL_SECONDS = 10 * 60
DOWNLOADS_DIR = Path.home() / "Downloads" / "LanDrop"
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

LOCAL_ROOM = "local"


@dataclass
class Transfer:
    transfer_id: str
    filename: str
    size: int
    sender_name: str
    sender_device_id: str
    status: str = "pending"  # pending -> accepted|declined -> receiving -> complete|failed
    created_at: float = field(default_factory=time.time)
    saved_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "filename": self.filename,
            "size": self.size,
            "sender_name": self.sender_name,
            "sender_device_id": self.sender_device_id,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class BrowserShare:
    share_id: str
    filename: str
    size: int
    content: bytes
    media_type: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + BROWSER_SHARE_TTL_SECONDS)

    def to_dict(self, url: str, qr_code: str) -> dict[str, Any]:
        return {
            "share_id": self.share_id,
            "filename": self.filename,
            "size": self.size,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "url": url,
            "qr_code": qr_code,
        }


def _local_ipv4() -> str:
    """Return the address other devices can use on the current LAN."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect performs route selection without sending application data.
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _qr_data_url(value: str) -> str:
    image = qrcode.make(value)
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def create_app(
    device_name: str | None = None,
    http_port: int = 53317,
    *,
    config_path: str | Path | None = None,
    download_dir: str | Path | None = None,
) -> Flaxon:
    app = Flaxon("landrop")
    settings = SettingsStore(config_path)
    active_name = device_name or settings.device_name
    active_download_dir = Path(download_dir).expanduser() if download_dir else settings.download_dir
    custom_download_dir = download_dir is not None or settings.download_dir != DOWNLOADS_DIR
    discovery = DiscoveryService(device_name=active_name, http_port=http_port)
    transfers: dict[str, Transfer] = {}
    history: dict[str, dict[str, Any]] = {}
    send_tasks: dict[str, asyncio.Task[Any]] = {}
    browser_shares: dict[str, BrowserShare] = {}
    lan_url = f"http://{_local_ipv4()}:{http_port}/"
    phone_qr_code = _qr_data_url(lan_url)

    app.state.discovery = discovery
    app.state.transfers = transfers
    app.state.history = history
    app.state.send_tasks = send_tasks
    app.state.browser_shares = browser_shares
    app.state.settings = settings
    app.state.device_name = active_name
    app.state.lan_url = lan_url
    app.state.phone_qr_code = phone_qr_code
    app.state.downloads_dir = active_download_dir
    app.state.custom_download_dir = custom_download_dir

    active_download_dir.mkdir(parents=True, exist_ok=True)
    app.use_templates(Jinax(str(TEMPLATE_DIR), auto_reload=True))
    app.mount_static("/static", str(STATIC_DIR))
    app.mount_module(ui, prefix="")

    @app.on_startup
    async def _start_discovery():
        await discovery.start()

    @app.on_shutdown
    async def _stop_discovery():
        await discovery.stop()

    # --- Identity & peers -------------------------------------------------

    @app.get("/api/me")
    async def me(request):
        return {
            "device_id": discovery.device_id,
            "name": app.state.device_name,
            "http_port": http_port,
        }

    @app.get("/api/peers")
    async def list_peers(request):
        return {"peers": discovery.snapshot()}

    # --- Transfer offer, called by the SENDER onto the RECEIVER's server --

    @app.post("/api/transfers")
    async def create_transfer(request):
        payload = await request.json()
        filename = payload.get("filename")
        size = payload.get("size")
        sender_name = payload.get("sender_name", "Unknown device")
        sender_device_id = payload.get("sender_device_id", "")

        if not filename or not isinstance(size, int) or size <= 0:
            return JSONResponse(
                {"error": "filename and a positive integer size are required"},
                status_code=422,
            )
        if size > MAX_TRANSFER_BYTES:
            return JSONResponse(
                {"error": f"file exceeds the {MAX_TRANSFER_BYTES} byte limit for v1"},
                status_code=413,
            )

        transfer = Transfer(
            transfer_id=str(uuid.uuid4()),
            filename=filename,
            size=size,
            sender_name=sender_name,
            sender_device_id=sender_device_id,
        )
        transfers[transfer.transfer_id] = transfer
        history[transfer.transfer_id] = {
            **transfer.to_dict(),
            "direction": "received",
        }

        await app.websocket_manager.broadcast_json(
            LOCAL_ROOM,
            {"event": "transfer.incoming", "transfer": transfer.to_dict()},
        )

        return JSONResponse(transfer.to_dict(), status_code=201)

    @app.get("/api/transfers/<transfer_id>")
    async def get_transfer(request, transfer_id: str):
        transfer = transfers.get(transfer_id)
        if transfer is None:
            return JSONResponse({"error": "transfer_not_found"}, status_code=404)
        return transfer.to_dict()

    # --- Accept / decline, called by the RECEIVER's own tray app onto ----
    # --- its OWN local server (never across the network) -----------------

    @app.post("/api/transfers/<transfer_id>/accept")
    async def accept_transfer(request, transfer_id: str):
        transfer = transfers.get(transfer_id)
        if transfer is None:
            return JSONResponse({"error": "transfer_not_found"}, status_code=404)
        if transfer.status != "pending":
            return JSONResponse({"error": "transfer_not_pending"}, status_code=409)

        transfer.status = "accepted"
        history[transfer_id].update(status=transfer.status)
        await app.websocket_manager.broadcast_json(
            f"transfer:{transfer_id}",
            {"event": "transfer.accepted", "transfer": transfer.to_dict()},
        )
        return transfer.to_dict()

    @app.post("/api/transfers/<transfer_id>/decline")
    async def decline_transfer(request, transfer_id: str):
        transfer = transfers.get(transfer_id)
        if transfer is None:
            return JSONResponse({"error": "transfer_not_found"}, status_code=404)
        if transfer.status != "pending":
            return JSONResponse({"error": "transfer_not_pending"}, status_code=409)

        transfer.status = "declined"
        history[transfer_id].update(status=transfer.status)
        await app.websocket_manager.broadcast_json(
            f"transfer:{transfer_id}",
            {"event": "transfer.declined", "transfer": transfer.to_dict()},
        )
        return transfer.to_dict()

    # --- Upload, called by the SENDER onto the RECEIVER's server, only ---
    # --- once the receiver has accepted --------------------------------

    @app.post("/api/transfers/<transfer_id>/upload")
    async def upload_transfer(request, transfer_id: str):
        transfer = transfers.get(transfer_id)
        if transfer is None:
            return JSONResponse({"error": "transfer_not_found"}, status_code=404)
        if transfer.status != "accepted":
            return JSONResponse({"error": "transfer_not_accepted"}, status_code=409)

        transfer.status = "receiving"
        history[transfer_id].update(status=transfer.status)
        body = await request.body()

        if len(body) != transfer.size:
            transfer.status = "failed"
            history[transfer_id].update(status=transfer.status)
            await app.websocket_manager.broadcast_json(
                LOCAL_ROOM,
                {"event": "transfer.failed", "transfer": transfer.to_dict()},
            )
            await app.websocket_manager.broadcast_json(
                f"transfer:{transfer_id}",
                {"event": "transfer.failed", "transfer": transfer.to_dict()},
            )
            return JSONResponse(
                {"error": "uploaded size did not match the declared size"},
                status_code=400,
            )

        safe_name = Path(transfer.filename).name  # strip any path components
        destination_dir = app.state.downloads_dir if app.state.custom_download_dir else DOWNLOADS_DIR
        destination_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_destination(destination_dir / safe_name)
        dest.write_bytes(body)
        transfer.saved_path = dest
        transfer.status = "complete"
        history[transfer_id].update(status=transfer.status)

        await app.websocket_manager.broadcast_json(
            LOCAL_ROOM,
            {"event": "transfer.complete", "transfer": transfer.to_dict()},
        )
        await app.websocket_manager.broadcast_json(
            f"transfer:{transfer_id}",
            {"event": "transfer.complete", "transfer": transfer.to_dict()},
        )
        return transfer.to_dict()

    @app.get("/api/history")
    async def transfer_history(request):
        items = sorted(history.values(), key=lambda item: item.get("created_at", 0), reverse=True)
        return {"transfers": items[:100]}

    @app.get("/api/settings")
    async def get_settings(request):
        return settings.as_dict()

    @app.post("/api/settings")
    async def update_settings(request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("settings must be a JSON object")
            settings.update(
                device_name=payload.get("device_name"),
                download_dir=payload.get("download_dir"),
            )
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        app.state.device_name = settings.device_name
        app.state.downloads_dir = settings.download_dir
        app.state.custom_download_dir = True
        discovery.device_name = settings.device_name
        return settings.as_dict()

    async def _send_bytes(job_id: str, filename: str, data: bytes, peer: dict[str, Any]) -> None:
        entry = history[job_id]
        base_url = f"http://{peer['address']}:{peer['http_port']}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                offer = await client.post(
                    f"{base_url}/api/transfers",
                    json={
                        "filename": filename,
                        "size": len(data),
                        "sender_name": app.state.device_name,
                        "sender_device_id": discovery.device_id,
                    },
                )
                offer.raise_for_status()
                remote_id = offer.json()["transfer_id"]
                entry.update(remote_transfer_id=remote_id, status="pending")

                ws_url = f"ws://{peer['address']}:{peer['http_port']}/ws/transfers/{remote_id}"
                async with websockets.connect(ws_url) as ws:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                event = json.loads(raw)
                status = event.get("transfer", {}).get("status", "declined")
                entry["status"] = status
                if status != "accepted":
                    return

                upload = await client.post(
                    f"{base_url}/api/transfers/{remote_id}/upload",
                    content=data,
                    timeout=120.0,
                )
                upload.raise_for_status()
                entry["status"] = "complete"
        except Exception as exc:
            entry.update(status="failed", error=str(exc))

    @app.post("/api/ui/send")
    async def send_from_ui(request):
        peer_id = request.query.get("peer_id")
        filename = request.query.get("filename", "unnamed-file").strip() or "unnamed-file"
        peer = next((item for item in discovery.snapshot() if item["device_id"] == peer_id), None)
        if peer is None:
            return JSONResponse({"error": "peer_not_found"}, status_code=404)
        body = await request.body()
        if not body:
            return JSONResponse({"error": "file_body_required"}, status_code=422)
        if len(body) > MAX_TRANSFER_BYTES:
            return JSONResponse({"error": "file exceeds the transfer limit"}, status_code=413)

        job_id = str(uuid.uuid4())
        history[job_id] = {
            "transfer_id": job_id,
            "filename": Path(filename).name,
            "size": len(body),
            "sender_name": app.state.device_name,
            "status": "starting",
            "direction": "sent",
            "created_at": time.time(),
        }
        task = asyncio.create_task(_send_bytes(job_id, Path(filename).name, body, peer))
        send_tasks[job_id] = task
        task.add_done_callback(lambda _: send_tasks.pop(job_id, None))
        return JSONResponse({"job_id": job_id, "status": "starting"}, status_code=202)

    # --- Temporary browser shares --------------------------------------

    def _active_share(share_id: str) -> BrowserShare | None:
        share = browser_shares.get(share_id)
        if share is None:
            return None
        if share.expires_at <= time.time():
            browser_shares.pop(share_id, None)
            return None
        return share

    @app.post("/api/ui/phone-share")
    async def create_phone_share(request):
        filename = Path(request.query.get("filename", "unnamed-file")).name or "unnamed-file"
        body = await request.body()
        if not body:
            return JSONResponse({"error": "file_body_required"}, status_code=422)
        if len(body) > MAX_TRANSFER_BYTES:
            return JSONResponse({"error": "file exceeds the transfer limit"}, status_code=413)

        share_id = uuid.uuid4().hex
        media_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
        if not media_type or any(char in media_type for char in "\r\n"):
            media_type = "application/octet-stream"
        share = BrowserShare(
            share_id=share_id,
            filename=filename,
            size=len(body),
            content=body,
            media_type=media_type,
        )
        browser_shares[share_id] = share
        url = f"{app.state.lan_url.rstrip('/')}/ui/receive/{share_id}"
        return JSONResponse(share.to_dict(url, _qr_data_url(url)), status_code=201)

    @app.get("/api/ui/phone-share/<share_id>/download")
    async def download_phone_share(request, share_id: str):
        share = _active_share(share_id)
        if share is None:
            return JSONResponse({"error": "share_not_found_or_expired"}, status_code=404)
        browser_shares.pop(share_id, None)
        safe_filename = share.filename.replace('"', "")
        return Response(
            share.content,
            media_type=share.media_type,
            headers={"content-disposition": f'attachment; filename="{safe_filename}"'},
        )

    # --- WebSockets ---------------------------------------------------

    @app.websocket("/ws/local")
    async def local_events(socket):
        """The tray app's own connection to its own local server.

        Drives the tray icon state and toast notifications: incoming
        transfer requests, and completion/failure of transfers this
        device is sending or receiving.
        """
        await socket.accept()
        await socket.join(LOCAL_ROOM)
        async for _ in socket.iter_json():
            pass  # this channel is server -> client only; ignore any input

    @app.websocket("/ws/transfers/<transfer_id>")
    async def transfer_events(socket, transfer_id: str):
        """Cross-device: the SENDER connects to the RECEIVER's server on
        this to be notified the moment the receiver accepts/declines/
        completes, instead of polling GET /api/transfers/<id>.
        """
        await socket.accept()
        await socket.join(f"transfer:{transfer_id}")
        async for _ in socket.iter_json():
            pass

    return app


def _unique_destination(path: Path) -> Path:
    """Avoid clobbering an existing file with the same name."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
