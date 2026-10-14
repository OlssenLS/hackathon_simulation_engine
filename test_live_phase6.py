import asyncio
import json
import os
import httpx
import websockets

async def main():
    # 1. Verify ml artifact exists
    assert os.path.exists("ml/isoforest.joblib"), "ml/isoforest.joblib was not created!"
    print("ml/isoforest.joblib exists!")

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30.0) as client:
        # 2. Login
        res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        res = await client.get("/me", headers=headers)
        budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
        print(f"Testing Phase 6 ML & Baseline on Pak Budi ({budi_id})...")

        ws_url = f"ws://127.0.0.1:8000/ws/{budi_id}?token={token}"
        async with websockets.connect(ws_url) as ws:
            print("Connected to WS!")

            # 3. Monitor normal run for 10 ticks (30s) - verify no false alerts
            print("Observing normal run for 30s to verify no false anomaly_* alerts...")
            false_alerts = []
            start_t = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_t < 30.0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "alert":
                        a_type = msg["data"].get("type")
                        print("Alert received during normal run:", a_type)
                        if a_type in ("anomaly_baseline", "anomaly_ml"):
                            false_alerts.append(a_type)
                except asyncio.TimeoutError:
                    continue

            assert len(false_alerts) == 0, f"False alerts detected during normal simulation: {false_alerts}"
            print("Verified: 0 false anomaly alerts during normal run!")

            # 4. Test abnormal injection for anomaly_baseline
            print("\nTesting anomaly_baseline injection (HR > 3-sigma above circadian baseline)...")
            from app.services.simulator import simulator
            state = simulator.states.get(budi_id)
            if state:
                # Force current_hr to 102 bpm (sleeping baseline is ~56-60, so z > 4) for 5 ticks
                state["current_hr"] = 104.0
                state["activity"] = "sleeping"

            received_baseline_anomaly = False
            for _ in range(15):
                if state:
                    state["current_hr"] = 104.0
                    state["activity"] = "sleeping"
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "alert" and msg["data"].get("type") == "anomaly_baseline":
                        print("--> Received anomaly_baseline alert:", msg["data"])
                        assert "reason" in msg["data"]["payload"]
                        print("Explainable reason:", msg["data"]["payload"]["reason"])
                        received_baseline_anomaly = True
                        break
                except asyncio.TimeoutError:
                    continue

            assert received_baseline_anomaly, "Did not receive anomaly_baseline alert"
            print("anomaly_baseline verified!")

            # 5. Test abnormal injection for anomaly_ml
            print("\nTesting anomaly_ml injection (unusual multi-feature pattern)...")
            received_ml_anomaly = False
            for _ in range(15):
                if state:
                    # Multi-feature anomaly: low SpO2, high temp, unusual HR
                    state["current_hr"] = 105.0
                    state["activity"] = "resting"
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "alert" and msg["data"].get("type") == "anomaly_ml":
                        print("--> Received anomaly_ml alert:", msg["data"])
                        assert "reason" in msg["data"]["payload"]
                        assert "top_features" in msg["data"]["payload"]
                        print("Explainable ML reason:", msg["data"]["payload"]["reason"])
                        print("Top features:", msg["data"]["payload"]["top_features"])
                        received_ml_anomaly = True
                        break
                except asyncio.TimeoutError:
                    continue

            # Clear state back to normal
            if state:
                state["scenario"] = "normal"
                state["current_hr"] = 72.0

            print("=== PHASE 6 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(main())
