# BeamLAN

Send files between devices on the same local network — instantly, with no
cloud, no account, and no cable. Think AirDrop, but for Windows (and,
eventually, any device on the same WiFi with a browser).

## How it works

- Every device running LanDrop broadcasts a small UDP beacon on the LAN
  (`app/discovery.py`) and listens for the same from everyone else, so
  devices find each other with zero configuration.
- Sending a file is a three-step handshake directly between two devices'
  local Flaxon servers (`app/server.py`): the sender offers the file
  (name + size), the receiver's tray app pops up an accept/decline
  prompt, and on accept the sender uploads the bytes directly over the
  LAN. No third-party server is ever involved.
- Both devices get live status over a WebSocket (`/ws/local` for the
  tray app's own UI updates, `/ws/transfers/<id>` for the sender to
  learn the moment the receiver accepts/declines/finishes) instead of
  polling.
- The tray app (`app/tray.py`) is a thin Windows shell around that
  server: it runs the server in the background and opens the Jinax web UI
  in an embedded desktop window. The same UI is also reachable from a
  phone browser on the LAN.
- The phone onboarding page (`/ui/connect`) displays the computer's LAN
  address as a QR code, so a phone can connect without typing an IP address.
- PC-to-phone sharing creates a random, ten-minute URL under
  `/ui/receive/<share-id>`. The phone downloads the file directly from the
  PC, and the download URL is removed after one successful download.

## What's been verified vs. what hasn't

This was built and tested from a Linux sandbox with no Windows desktop
and no GUI display available, so testing split naturally into two tiers:

**Verified, live, end-to-end:**
- Full transfer lifecycle (offer → accept → upload → saved to disk)
  via `flaxon.testing.AsyncTestClient`.
- Real-time WebSocket push: an HTTP request on one connection
  correctly triggers an immediate broadcast received by a separate,
  already-connected WebSocket client (`ws_test.py` in this repo is
  the actual test script used).
- Placeholder MSIX tile assets generate at the exact required sizes.
- Jinax pages render for desktop send, incoming approval, history, settings,
  phone onboarding, temporary PC-to-phone receiving, tutorial, and the phone
  upload flow; settings persist across app instances.

**Written carefully, following well-established patterns, but not yet
exercised on a real Windows machine:**
- `app/tray.py` — pystray picks its backend (X11/Win32/Cocoa) at
  import time; there's no X server here, so this only imports
  successfully on an actual desktop. The logic (threading model,
  event handling, dialog flow) is correct against the tested server
  API, but the *GUI interaction itself* — does the tray icon actually
  appear, do the accept/decline dialogs behave well when triggered
  from a background thread, does `icon.notify()` produce a real
  Windows toast — needs a real Windows smoke test before shipping.
- `.github/workflows/build-msix.yml` — follows the standard, widely
  documented pattern for packaging a PyInstaller exe as MSIX via the
  Windows SDK's `makeappx.exe`/`signtool.exe`, but has not actually
  been run (no Windows CI runner available here). Run it once via
  `workflow_dispatch` and check the artifact before relying on it.

**Recommended first real test**, in order:
1. `python run_dev.py` on Windows, confirm the tray icon appears and
   `Send File...` / `Open Downloads Folder` work locally.
2. Run it on two machines on the same WiFi, confirm they discover each
   other and a transfer completes end-to-end.
3. Trigger the GitHub Actions workflow manually, download the `.msix`
   artifact, and sideload-install it (enable Developer Mode in Windows
   Settings, then double-click the `.msix`).

## Local development

```bash
pip install -r requirements.txt
python run_dev.py       # starts just the server, no tray icon, for API testing
python main.py           # starts the desktop window + tray app (Windows only)
```

## Phone tutorial

1. Start LanDrop on the Windows PC and open **Connect phone** from the tray
   menu or `/ui/connect`.
2. Connect the phone to the same Wi-Fi or hotspot. Scan the QR code shown on
   the PC, or open the displayed LAN address manually.
3. To send phone-to-PC, choose a file on the phone and press **Send**. Approve
   the request in the Windows browser page before the file is saved.
4. To send PC-to-phone, open **Send**, choose **Create phone share**, and scan
   the generated QR code. The phone opens a temporary page with a download
   button.
5. Use **How to use LanDrop** for the built-in troubleshooting checklist.

The PC-to-phone page is temporary and one-time. It is intended for nearby
sharing, not for publishing files to the public internet.

## Known limitations (v1, intentional scope cuts)

- File bodies are buffered fully in memory rather than streamed
  (`Request.body()` in this Flaxon version doesn't expose a streaming
  API) — fine for the photos/docs this is aimed at, capped at 200MB in
  `server.py`, but large video files would need a follow-up to switch
  to chunked/streaming upload.
- UDP broadcast discovery doesn't cross subnets/VLANs. If that turns
  out to matter (e.g. corporate WiFi with client isolation), swap
  `discovery.py` for mDNS (the `zeroconf` package) without touching
  `server.py` — discovery and transfer are cleanly separated.
- The browser UI is intentionally local-network scoped. It has no account
  layer or internet relay; expose it outside the LAN only behind a separate
  authenticated gateway. QR links and temporary phone shares are bearer links
  for trusted local networks, expire after ten minutes, and can be downloaded
  once. They are not an internet-sharing or access-control system.

## Publishing to the Microsoft Store

CI produces an installable `.msix`, but the Store submission itself
needs a human in the loop and can't be safely scripted end-to-end:

1. Reserve your app name and get your Publisher CN string at
   [Partner Center](https://partner.microsoft.com/dashboard).
2. The reserved identity is already applied in
   `packaging/AppxManifest.xml`: `HappyRecorder3D.lan-airdrop` with
   publisher `CN=50CA2AC2-0155-44AC-B2B0-47100A3FB6E2`.
3. The generated LanDrop Store logo has been resized into the MSIX tile
   assets in `packaging/Assets/`. The original logo and promotional images
   are in `store-assets/` for Partner Center screenshots and listing art.
   Review them against [Microsoft's icon guidance](https://learn.microsoft.com/windows/apps/design/style/app-icons-and-logos).
4. Download the `.msix` artifact from a workflow run and upload it
   through Partner Center's submission flow.

## Project layout

```
app/
  discovery.py   # UDP broadcast peer discovery
  config.py      # persistent device name and download-folder settings
  modules/       # FlaxonModule-owned web routes
  server.py      # Flaxon backend, Jinax UI, HTTP + WebSockets
  tray.py        # Windows tray shell; opens the browser UI
  templates/     # Jinax pages for desktop and phone workflows
  static/        # UI CSS and browser-side transfer logic
main.py          # packaged entry point
run_dev.py       # dev entry point: server only, no tray icon
packaging/
  AppxManifest.xml
  generate_placeholder_assets.py
  Assets/
store-assets/          # Store logo and two promotional screenshots
.github/workflows/build-msix.yml
```
