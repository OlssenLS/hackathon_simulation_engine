import asyncio
import json
import httpx
import websockets
from app.main import app, lifespan

async def run_phase3_test():
    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 1. Login as Caregiver
            res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
            assert res.status_code == 200
            token = res.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            # 2. Get Pak Budi ID
            res = await client.get("/me", headers=headers)
            budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
            print("Pak Budi ID:", budi_id)

            # 3. Test GET /elders/{id}/vitals/latest
            res = await client.get(f"/elders/{budi_id}/vitals/latest", headers=headers)
            print("GET /vitals/latest status:", res.status_code, res.json())
            assert res.status_code == 200
            latest = res.json()
            assert "vitals" in latest
            assert "bp" in latest
            assert "glucose" in latest
            assert "status" in latest

            # 4. Test GET /elders/{id}/location/latest and trail
            res = await client.get(f"/elders/{budi_id}/location/latest", headers=headers)
            print("GET /location/latest:", res.status_code, res.json())
            assert res.status_code == 200
            assert "lat" in res.json()

            res = await client.get(f"/elders/{budi_id}/location/trail?minutes=60", headers=headers)
            print("GET /location/trail points:", res.status_code, len(res.json()))
            assert res.status_code == 200

            # 5. Test manual measurement entry
            res = await client.post(
                f"/elders/{budi_id}/measurements",
                headers=headers,
                json={"kind": "bp", "value1": 125, "value2": 82}
            )
            print("POST manual measurement:", res.status_code, res.json())
            assert res.status_code == 201

            # 6. Test WebSocket directly with starlette test client or live connection
            # We will start uvicorn in a background thread or test with a simulated client
            print("=== Testing WebSocket ===")
            from starlette.testclient import TestClient
            with TestClient(app) as test_client:
                with test_client.websocket_connect(f"/ws/{budi_id}?token={token}") as ws:
                    print("Connected to WebSocket successfully!")
                    # Wait for vitals event
                    received_vitals = False
                    received_location = False
                    for _ in range(10):
                        msg_text = ws.receive_text()
                        msg = json.loads(msg_text)
                        print(f"WS Received: type={msg.get('type')}")
                        if msg.get("type") == "vitals":
                            received_vitals = True
                            assert "hr" in msg["data"]
                            assert "spo2" in msg["data"]
                            assert "activity" in msg["data"]
                        elif msg.get("type") == "location":
                            received_location = True
                            assert "lat" in msg["data"]
                        if received_vitals and received_location:
                            break

                    assert received_vitals, "Did not receive vitals event over WebSocket"
                    assert received_location, "Did not receive location event over WebSocket"
                    print("WS vitals and location verified!")

                    # Test ping-pong
                    ws.send_text(json.dumps({"type": "ping"}))
                    reply = json.loads(ws.receive_text())
                    assert reply.get("type") == "pong"
                    print("Ping-pong verified!")

            print("=== PHASE 3 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(run_phase3_test())
