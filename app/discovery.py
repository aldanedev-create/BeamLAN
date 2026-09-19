"""LAN peer discovery for LanDrop.

Uses a simple UDP broadcast beacon: every device running the app
periodically shouts "I'm here, here's my name and my HTTP port" on the
local subnet, and listens for the same beacon from everyone else. No
central server, no internet access, no account -- just devices on the
same WiFi/LAN finding each other.

This is deliberately simple (broadcast + in-memory peer table with a
staleness timeout) rather than full mDNS/Bonjour, to keep the v1 small
and avoid extra native dependencies. Swapping in zeroconf/mDNS later
is a contained change if this needs to work across subnets or VLANs
where broadcast doesn't reach.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import uuid
from dataclasses import dataclass, field

BEACON_PORT = 51820
BEACON_INTERVAL_SECONDS = 3.0
PEER_STALE_AFTER_SECONDS = 10.0


@dataclass
class Peer:
    device_id: str
    name: str
    address: str
    http_port: int
    last_seen: float = field(default_factory=time.time)

    def is_stale(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_seen) > PEER_STALE_AFTER_SECONDS

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "address": self.address,
            "http_port": self.http_port,
        }


class DiscoveryService:
    """Broadcasts this device's presence and tracks peers seen on the LAN."""

    def __init__(self, device_name: str, http_port: int):
        self.device_id = str(uuid.uuid4())
        self.device_name = device_name
        self.http_port = http_port
        self.peers: dict[str, Peer] = {}
        self._send_sock: socket.socket | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._send_sock.setblocking(False)

        self._tasks.append(asyncio.create_task(self._broadcast_loop()))
        self._tasks.append(asyncio.create_task(self._listen_loop()))
        self._tasks.append(asyncio.create_task(self._reap_stale_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._send_sock is not None:
            self._send_sock.close()
            self._send_sock = None

    def snapshot(self) -> list[dict]:
        return [p.to_dict() for p in self.peers.values() if not p.is_stale()]

    async def _broadcast_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            payload = json.dumps(
                {
                    "type": "landrop.beacon",
                    "device_id": self.device_id,
                    "name": self.device_name,
                    "http_port": self.http_port,
                }
            ).encode("utf-8")
            try:
                await loop.sock_sendto(
                    self._send_sock, payload, ("255.255.255.255", BEACON_PORT)
                )
            except OSError:
                # Network briefly unavailable (e.g. WiFi reconnecting);
                # just try again on the next tick.
                pass
            await asyncio.sleep(BEACON_INTERVAL_SECONDS)

    async def _listen_loop(self) -> None:
        loop = asyncio.get_running_loop()
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        recv_sock.setblocking(False)
        recv_sock.bind(("0.0.0.0", BEACON_PORT))

        try:
            while True:
                try:
                    data, addr = await loop.sock_recvfrom(recv_sock, 2048)
                except OSError:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue

                if msg.get("type") != "landrop.beacon":
                    continue
                if msg.get("device_id") == self.device_id:
                    continue  # our own broadcast, echoed back

                peer = Peer(
                    device_id=msg["device_id"],
                    name=msg.get("name", "Unknown device"),
                    address=addr[0],
                    http_port=int(msg.get("http_port", 0)),
                )
                self.peers[peer.device_id] = peer
        finally:
            recv_sock.close()

    async def _reap_stale_loop(self) -> None:
        while True:
            await asyncio.sleep(PEER_STALE_AFTER_SECONDS)
            now = time.time()
            stale_ids = [pid for pid, p in self.peers.items() if p.is_stale(now)]
            for pid in stale_ids:
                del self.peers[pid]
