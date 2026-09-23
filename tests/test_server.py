"""Tests for app/server.py: the transfer lifecycle and its error paths.

These exercise the same HTTP surface verified live during development
(see README.md "What's been verified"), formalized so they're
rerunnable and catch regressions. WebSocket push was verified live via
ws_test.py against a running uvicorn server -- flaxon.testing's
AsyncTestClient doesn't support WebSocket testing, so that path isn't
covered by this automated suite yet; that's a good follow-up if a
WebSocket-capable test client becomes available.
"""

import pytest

from flaxon.testing import AsyncTestClient

from app.server import DOWNLOADS_DIR, MAX_TRANSFER_BYTES, create_app


@pytest.fixture
def client():
    app = create_app("Test-PC", 9999)
    return AsyncTestClient(app)


@pytest.mark.asyncio
async def test_me_returns_device_identity(client):
    r = await client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Test-PC"
    assert body["http_port"] == 9999
    assert "device_id" in body


@pytest.mark.asyncio
async def test_peers_starts_empty(client):
    r = await client.get("/api/peers")
    assert r.status_code == 200
    assert r.json() == {"peers": []}


@pytest.mark.asyncio
async def test_create_transfer_requires_filename_and_size(client):
    r = await client.post("/api/transfers", json_data={"filename": "x.txt"})
    assert r.status_code == 422

    r = await client.post("/api/transfers", json_data={"size": 10})
    assert r.status_code == 422

    r = await client.post("/api/transfers", json_data={"filename": "x.txt", "size": -1})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_transfer_rejects_oversized_file(client):
    r = await client.post(
        "/api/transfers",
        json_data={"filename": "huge.mov", "size": MAX_TRANSFER_BYTES + 1},
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_create_transfer_success(client):
    r = await client.post(
        "/api/transfers",
        json_data={
            "filename": "photo.png",
            "size": 5,
            "sender_name": "Sender-Phone",
            "sender_device_id": "abc",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["filename"] == "photo.png"
    assert "transfer_id" in body


@pytest.mark.asyncio
async def test_get_unknown_transfer_404s(client):
    r = await client.get("/api/transfers/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_full_lifecycle_offer_accept_upload(client, tmp_path, monkeypatch):
    # Redirect downloads to a temp dir so this test doesn't write into
    # the developer's real Downloads folder.
    monkeypatch.setattr("app.server.DOWNLOADS_DIR", tmp_path)

    create = await client.post(
        "/api/transfers",
        json_data={"filename": "report.pdf", "size": 5, "sender_name": "Sender"},
    )
    transfer_id = create.json()["transfer_id"]

    accept = await client.post(f"/api/transfers/{transfer_id}/accept")
    assert accept.status_code == 200
    assert accept.json()["status"] == "accepted"

    upload = await client.post(
        f"/api/transfers/{transfer_id}/upload", content=b"hello"
    )
    assert upload.status_code == 200
    assert upload.json()["status"] == "complete"

    saved = tmp_path / "report.pdf"
    assert saved.exists()
    assert saved.read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_upload_rejects_size_mismatch(client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.server.DOWNLOADS_DIR", tmp_path)

    create = await client.post(
        "/api/transfers",
        json_data={"filename": "x.bin", "size": 100, "sender_name": "Sender"},
    )
    transfer_id = create.json()["transfer_id"]
    await client.post(f"/api/transfers/{transfer_id}/accept")

    upload = await client.post(
        f"/api/transfers/{transfer_id}/upload", content=b"too short"
    )
    assert upload.status_code == 400

    status = await client.get(f"/api/transfers/{transfer_id}")
    assert status.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_upload_before_accept_is_rejected(client):
    create = await client.post(
        "/api/transfers",
        json_data={"filename": "x.bin", "size": 5, "sender_name": "Sender"},
    )
    transfer_id = create.json()["transfer_id"]

    upload = await client.post(f"/api/transfers/{transfer_id}/upload", content=b"hello")
    assert upload.status_code == 409


@pytest.mark.asyncio
async def test_decline_transfer(client):
    create = await client.post(
        "/api/transfers",
        json_data={"filename": "x.bin", "size": 5, "sender_name": "Sender"},
    )
    transfer_id = create.json()["transfer_id"]

    decline = await client.post(f"/api/transfers/{transfer_id}/decline")
    assert decline.status_code == 200
    assert decline.json()["status"] == "declined"


@pytest.mark.asyncio
async def test_cannot_accept_twice(client):
    create = await client.post(
        "/api/transfers",
        json_data={"filename": "x.bin", "size": 5, "sender_name": "Sender"},
    )
    transfer_id = create.json()["transfer_id"]

    first = await client.post(f"/api/transfers/{transfer_id}/accept")
    assert first.status_code == 200

    second = await client.post(f"/api/transfers/{transfer_id}/accept")
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_filename_path_traversal_is_stripped(client, tmp_path, monkeypatch):
    """A malicious/buggy sender offering a filename with path components
    (e.g. "../../evil.exe") must not be able to write outside the
    downloads directory."""
    monkeypatch.setattr("app.server.DOWNLOADS_DIR", tmp_path)

    create = await client.post(
        "/api/transfers",
        json_data={
            "filename": "../../evil.txt",
            "size": 3,
            "sender_name": "Sender",
        },
    )
    transfer_id = create.json()["transfer_id"]
    await client.post(f"/api/transfers/{transfer_id}/accept")
    upload = await client.post(
        f"/api/transfers/{transfer_id}/upload", content=b"bad"
    )
    assert upload.status_code == 200

    # Should have landed inside tmp_path as "evil.txt", not escaped it.
    assert (tmp_path / "evil.txt").exists()
    assert not any(p.name == "evil.txt" for p in tmp_path.parent.glob("evil.txt"))


@pytest.mark.asyncio
async def test_jinax_ui_pages_render(client):
    for path in ("/", "/ui/send", "/ui/connect", "/ui/history", "/ui/settings", "/ui/tutorial"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "BeamLAN" in response.text


@pytest.mark.asyncio
async def test_connect_page_contains_phone_qr(client):
    response = await client.get("/ui/connect")
    assert response.status_code == 200
    assert "Phone onboarding" in response.text
    assert "data:image/png;base64," in response.text


@pytest.mark.asyncio
async def test_tutorial_images_are_served(client):
    tutorial = await client.get("/ui/tutorial")
    assert tutorial.status_code == 200
    assert "landrop-pc-transfer.png" in tutorial.text
    assert "landrop-phone-qr.png" in tutorial.text

    for asset in ("landrop-logo.png", "landrop-pc-transfer.png", "landrop-phone-qr.png"):
        response = await client.get(f"/static/{asset}")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_phone_share_is_one_time_download(client):
    created = await client.post(
        "/api/ui/phone-share?filename=photo.png",
        content=b"image-bytes",
        headers={"content-type": "image/png"},
    )
    assert created.status_code == 201
    share = created.json()
    assert share["filename"] == "photo.png"
    assert share["url"].endswith(f"/ui/receive/{share['share_id']}")
    assert share["qr_code"].startswith("data:image/png;base64,")

    page = await client.get(f"/ui/receive/{share['share_id']}")
    assert page.status_code == 200
    assert "photo.png" in page.text

    download = await client.get(f"/api/ui/phone-share/{share['share_id']}/download")
    assert download.status_code == 200
    assert download.content == b"image-bytes"
    assert download.headers["content-type"] == "image/png"

    second_download = await client.get(f"/api/ui/phone-share/{share['share_id']}/download")
    assert second_download.status_code == 404


@pytest.mark.asyncio
async def test_expired_phone_share_is_not_available(client):
    created = await client.post("/api/ui/phone-share?filename=x.txt", content=b"x")
    share_id = created.json()["share_id"]
    client.app.state.browser_shares[share_id].expires_at = 0

    page = await client.get(f"/ui/receive/{share_id}")
    assert page.status_code == 404
    assert "This transfer expired" in page.text


@pytest.mark.asyncio
async def test_incoming_ui_uses_existing_transfer(client):
    create = await client.post(
        "/api/transfers",
        json_data={"filename": "photo.jpg", "size": 10, "sender_name": "Phone"},
    )
    transfer_id = create.json()["transfer_id"]

    response = await client.get(f"/ui/incoming/{transfer_id}")
    assert response.status_code == 200
    assert "photo.jpg" in response.text
    assert "Phone" in response.text


@pytest.mark.asyncio
async def test_ui_send_rejects_unknown_peer(client):
    response = await client.post(
        "/api/ui/send?peer_id=missing&filename=x.txt",
        content=b"hello",
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_settings_persist_between_app_instances(tmp_path):
    config_path = tmp_path / "settings.json"
    first = create_app("Test-PC", 9999, config_path=config_path)
    first_client = AsyncTestClient(first)
    response = await first_client.post(
        "/api/settings",
        json_data={"device_name": "Office-PC", "download_dir": str(tmp_path / "received")},
    )
    assert response.status_code == 200

    second = create_app(None, 9998, config_path=config_path)
    second_client = AsyncTestClient(second)
    loaded = await second_client.get("/api/settings")
    assert loaded.json()["device_name"] == "Office-PC"
    assert loaded.json()["download_dir"] == str(tmp_path / "received")
