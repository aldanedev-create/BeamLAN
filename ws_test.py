import asyncio
import json

import httpx
import websockets


async def main():
    async with websockets.connect("ws://127.0.0.1:8600/ws/local") as ws:
        print("connected to /ws/local", flush=True)

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:8600/api/transfers",
                json={
                    "filename": "report.pdf",
                    "size": 3,
                    "sender_name": "Sender-Phone",
                    "sender_device_id": "xyz",
                },
            )
            print("create_transfer:", r.status_code, r.json(), flush=True)

        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        print("received on /ws/local:", msg, flush=True)
        parsed = json.loads(msg)
        assert parsed["event"] == "transfer.incoming", parsed


if __name__ == "__main__":
    asyncio.run(main())
