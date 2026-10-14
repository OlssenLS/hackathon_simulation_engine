import asyncio
import json
import httpx
import websockets

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
        # 1. Health check
        res = await client.get("/health")
        print("Health check:", res.status_code, res.json())
        assert res.status_code == 200

        # 2. Login Caregiver
        res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
        assert res.status_code == 200
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Get Pak Budi ID
        res = await client.get("/me", headers=headers)
        budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
        print("Pak Budi ID:", budi_id)

        # 4. Check latest vitals before WS
        res = await client.get(f"/elders/{budi_id}/vitals/latest", headers=headers)
        print("GET /vitals/latest:", res.status_code, res.json())
        assert res.status_code == 200

        # 5. Connect to WebSocket
        ws_url = f"ws://127.0.0.1:8000/ws/{budi_id}?token={token}"
        print(f"Connecting to WebSocket at {ws_url}...")
        async with websockets.connect(ws_url) as ws:
            print("Connected! Listening for vitals stream (3s ticks)...")
            received_vitals = False
            received_location = False
            last_ws_vitals = None

            for _ in range(10):
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                msg = json.loads(msg_raw)
                print(f"Received WS event: type={msg.get('type')}, elder_id={msg.get('elder_id')}")
                if msg.get("type") == "vitals":
                    received_vitals = True
                    last_ws_vitals = msg["data"]
                    print("Vitals data received:", last_ws_vitals)
                elif msg.get("type") == "location":
                    received_location = True
                    print("Location data received:", msg["data"])

                if received_vitals and received_location:
                    break

            assert received_vitals, "Failed to receive vitals via WebSocket!"
            assert received_location, "Failed to receive location via WebSocket!"

            # Test ping / pong
            await ws.send(json.dumps({"type": "ping"}))
            pong_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            pong_msg = json.loads(pong_raw)
            print("Pong received:", pong_msg)
            assert pong_msg.get("type") == "pong"

        # 6. Verify GET /elders/{id}/vitals/latest matches streamed vitals
        res = await client.get(f"/elders/{budi_id}/vitals/latest", headers=headers)
        latest_data = res.json()
        print("Verified /vitals/latest after stream:", latest_data["vitals"])
        assert latest_data["vitals"] is not None
        # Vitals should be very close or match
        assert abs(latest_data["vitals"]["hr"] - last_ws_vitals["hr"]) < 5.0

        print("=== PHASE 3 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(main())
