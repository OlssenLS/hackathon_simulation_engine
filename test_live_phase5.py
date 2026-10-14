import asyncio
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import httpx
import websockets

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")

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
        print(f"Testing Phase 5 on Pak Budi ({budi_id})...")

        # 3. List medications
        res = await client.get(f"/elders/{budi_id}/medications", headers=headers)
        print("GET medications status:", res.status_code, "count:", len(res.json()))
        assert res.status_code == 200
        assert len(res.json()) >= 2

        # 4. Check adherence
        res = await client.get(f"/elders/{budi_id}/medications/adherence?days=7", headers=headers)
        print("GET adherence:", res.status_code, res.json())
        assert res.status_code == 200
        adh = res.json()
        assert "rate" in adh
        assert "taken" in adh

        # 5. Doctor appointments CRUD
        appt_payload = {
            "doctor": "dr. Zaidul Akbar",
            "location": "Klinik Herbal Sehat",
            "ts": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            "notes": "Konsultasi nutrisi lansia"
        }
        res = await client.post(f"/elders/{budi_id}/appointments", json=appt_payload, headers=headers)
        print("POST appointment:", res.status_code, res.json()["id"])
        assert res.status_code == 201
        appt_id = res.json()["id"]

        res = await client.get(f"/elders/{budi_id}/appointments?upcoming=true", headers=headers)
        assert res.status_code == 200
        assert any(a["id"] == appt_id for a in res.json())

        # Update appointment
        res = await client.put(f"/elders/{budi_id}/appointments/{appt_id}", json={"notes": "Diundur 30 menit"}, headers=headers)
        assert res.status_code == 200
        assert res.json()["notes"] == "Diundur 30 menit"

        # Delete appointment
        res = await client.delete(f"/elders/{budi_id}/appointments/{appt_id}", headers=headers)
        assert res.status_code == 200
        print("Doctor appointments CRUD verified!")

        # 6. WebSocket for Med events
        ws_url = f"ws://127.0.0.1:8000/ws/{budi_id}?token={token}"
        async with websockets.connect(ws_url) as ws:
            print("Connected to WS room!")

            # Schedule a medication for right now (local time)
            local_now = datetime.now(JAKARTA_TZ)
            t_now_str = local_now.strftime("%H:%M")
            print(f"Creating test medication with time: {t_now_str}")

            med_payload = {
                "name": "Vitamin C 500mg Test",
                "dose": "1 tablet",
                "times": [t_now_str],
                "days": [],
                "notes": "Minum sesudah makan"
            }
            res = await client.post(f"/elders/{budi_id}/medications", json=med_payload, headers=headers)
            print("POST medication:", res.status_code, res.json()["name"])
            assert res.status_code == 201
            test_med_id = res.json()["id"]

            # Check med-logs: should have a pending log
            res = await client.get(f"/elders/{budi_id}/med-logs", headers=headers)
            print("GET med-logs count:", len(res.json()))
            logs = res.json()
            test_log = next((l for l in logs if l["medication_id"] == test_med_id), None)
            assert test_log is not None, "Test medication log was not created!"
            print(f"Pending log found: id={test_log['id']}, status={test_log['status']}")
            assert test_log["status"] == "pending"

            # Wait for WS to emit due or tick
            print("Waiting for due med event over WS...")
            received_due = False
            for _ in range(12):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "med":
                        print(f"--> Received med event: {msg['data']}")
                        if msg["data"].get("log_id") == test_log["id"]:
                            received_due = True
                            break
                except asyncio.TimeoutError:
                    continue

            print(f"Med due received over WS: {received_due}")

            # Test confirm
            print("Confirming med-log...")
            res = await client.post(f"/med-logs/{test_log['id']}/confirm", headers=headers)
            assert res.status_code == 200
            assert res.json()["status"] == "taken"
            print("Confirm verified! status is now taken.")

            # Test missed med log: create another scheduled in past (>30 min ago)
            past_dt = datetime.now(timezone.utc) - timedelta(minutes=45)
            past_local = past_dt.astimezone(JAKARTA_TZ)
            t_past_str = past_local.strftime("%H:%M")
            print(f"Creating past medication for missed check: {t_past_str}")

            res = await client.post(
                f"/elders/{budi_id}/medications",
                json={
                    "name": "Obat Darah Tinggi Malam (Missed Test)",
                    "dose": "1 tablet",
                    "times": [t_past_str],
                    "days": [],
                },
                headers=headers
            )
            assert res.status_code == 201
            missed_med_id = res.json()["id"]

            # Wait for scheduler tick to detect missed and emit alert + med missed
            print("Waiting for missed detection...")
            received_missed_event = False
            received_missed_alert = False

            for _ in range(15):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "med" and msg["data"].get("status") == "missed":
                        print("--> Received med missed WS event:", msg["data"])
                        received_missed_event = True
                    elif msg.get("type") == "alert" and msg["data"].get("type") == "med_missed":
                        print("--> Received med_missed alert:", msg["data"]["message"])
                        received_missed_alert = True
                    if received_missed_event and received_missed_alert:
                        break
                except asyncio.TimeoutError:
                    continue

            assert received_missed_event, "Did not receive med missed WS event"
            assert received_missed_alert, "Did not receive med_missed alert"

            print("=== PHASE 5 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(main())
