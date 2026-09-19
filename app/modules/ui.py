"""Jinax pages shared by the desktop tray and phone browsers."""

from __future__ import annotations

import time

from flaxon.modules import FlaxonModule


ui = FlaxonModule("ui")


def _state(request):
    return request.app.state


@ui.get("/")
@ui.get("/ui/")
async def home(request):
    state = _state(request)
    return await request.render("ui/home.html", {
        "device_name": state.device_name,
        "settings": state.settings.as_dict(),
        "lan_url": state.lan_url,
    })


@ui.get("/ui/send")
async def send_picker(request):
    state = _state(request)
    return await request.render("ui/send.html", {
        "device_name": state.device_name,
        "lan_url": state.lan_url,
    })


@ui.get("/ui/connect")
async def connect_phone(request):
    state = _state(request)
    return await request.render("ui/connect.html", {
        "device_name": state.device_name,
        "lan_url": state.lan_url,
        "qr_code": state.phone_qr_code,
    })


@ui.get("/ui/tutorial")
async def tutorial(request):
    return await request.render("ui/tutorial.html", {"device_name": _state(request).device_name})


@ui.get("/ui/incoming/<transfer_id>")
async def incoming(request, transfer_id: str):
    transfer = _state(request).transfers.get(transfer_id)
    if transfer is None:
        response = await request.render("ui/incoming.html", {"transfer_id": transfer_id, "transfer": None})
        response.status_code = 404
        return response
    return await request.render("ui/incoming.html", {
        "transfer_id": transfer_id,
        "transfer": transfer.to_dict(),
    })


@ui.get("/ui/receive/<share_id>")
async def receive_share(request, share_id: str):
    state = _state(request)
    share = state.browser_shares.get(share_id)
    if share is not None and share.expires_at <= time.time():
        state.browser_shares.pop(share_id, None)
        share = None
    response = await request.render("ui/receive.html", {
        "device_name": state.device_name,
        "share": share.to_dict(
            f"{state.lan_url.rstrip('/')}/ui/receive/{share_id}",
            "",
        ) if share is not None else None,
        "download_url": f"{state.lan_url.rstrip('/')}/api/ui/phone-share/{share_id}/download",
    })
    if share is None:
        response.status_code = 404
    return response


@ui.get("/ui/history")
async def history(request):
    return await request.render("ui/history.html", {"device_name": _state(request).device_name})


@ui.get("/ui/settings")
async def settings(request):
    state = _state(request)
    return await request.render("ui/settings.html", {
        "device_name": state.device_name,
        "settings": state.settings.as_dict(),
    })
