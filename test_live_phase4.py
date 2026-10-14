import asyncio
import json
import httpx
import websockets

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30.0) as client:
        # 1. Login Caregiver
        res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
        assert res.status_code == 200
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Get Pak Budi ID
        res = await client.get("/me", headers=headers)
        budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
        print(f"Testing scenarios on Pak Budi ({budi_id})...")

        # 3. Connect WebSocket client
        ws_url = f"ws://127.0.0.1:8000/ws/{budi_id}?token={token}"
        async with websockets.connect(ws_url) as ws:
            print("Connected to WS room!")

            async def wait_for_alert(expected_type: str, timeout: float = 22.0):
                end_time = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < end_time:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        msg = json.loads(raw)
                        if msg.get("type") == "alert":
                            alert_type = msg["data"].get("type")
                            print(f"--> Received Alert: type={alert_type}, severity={msg['data'].get('severity')}, msg={msg['data'].get('message')}")
                            if alert_type == expected_type:
                                return msg["data"]
                    except asyncio.TimeoutError:
                        continue
                raise TimeoutError(f"Did not receive alert {expected_type} within {timeout}s")

            # Test A: Manual SOS
            print("\n--- Test A: Manual SOS ---")
            res = await client.post(f"/elders/{budi_id}/sos", json={"note": "Tolong cepat datang!"}, headers=headers)
            assert res.status_code == 201
            sos_alert = await wait_for_alert("sos_manual", timeout=5.0)
            assert sos_alert["type"] == "sos_manual"
            print("Manual SOS verified!")

            # Test Ack for Manual SOS
            res = await client.post(f"/alerts/{sos_alert['id']}/ack", headers=headers)
            assert res.status_code == 200
            assert res.json()["acknowledged"] is True
            print("Ack verified!")

            # Test B: Fall Scenario
            print("\n--- Test B: Fall Scenario (fall_suspected within 20s) ---")
            res = await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "fall", "duration_s": 25}, headers=headers)
            assert res.status_code == 200
            fall_alert = await wait_for_alert("fall_suspected", timeout=20.0)
            assert fall_alert["severity"] == "critical"
            print("Fall detection verified!")

            # Clear to normal
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)
            await asyncio.sleep(2)

            # Test C: Hypoxia
            print("\n--- Test C: Hypoxia (spo2_low) ---")
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "hypoxia", "duration_s": 20}, headers=headers)
            hypoxia_alert = await wait_for_alert("spo2_low", timeout=15.0)
            print("Hypoxia verified!")

            # Clear to normal
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)
            await asyncio.sleep(2)

            # Test D: Tachycardia
            print("\n--- Test D: Tachycardia (hr_high) ---")
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "tachycardia", "duration_s": 20}, headers=headers)
            tach_alert = await wait_for_alert("hr_high", timeout=15.0)
            print("Tachycardia verified!")

            # Clear to normal
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)
            await asyncio.sleep(2)

            # Test E: Hypertensive
            print("\n--- Test E: Hypertensive (bp_high) ---")
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "hypertensive", "duration_s": 10}, headers=headers)
            bp_alert = await wait_for_alert("bp_high", timeout=5.0)
            print("Hypertensive verified!")

            # Test F: Hypoglycemia
            print("\n--- Test F: Hypoglycemia (glucose_low) ---")
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "hypoglycemia", "duration_s": 10}, headers=headers)
            g_alert = await wait_for_alert("glucose_low", timeout=5.0)
            print("Hypoglycemia verified!")

            # Test G: Wander
            print("\n--- Test G: Wander (geofence_exit) ---")
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "wander", "duration_s": 20}, headers=headers)
            wander_alert = await wait_for_alert("geofence_exit", timeout=15.0)
            print("Wander / geofence exit verified!")

            # Clear back to normal
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)

            # Test GET /elders/{id}/alerts
            res = await client.get(f"/elders/{budi_id}/alerts?limit=20", headers=headers)
            print("\nGET /elders/{id}/alerts count:", len(res.json()))
            assert len(res.json()) >= 7

            print("=== PHASE 4 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(main())
