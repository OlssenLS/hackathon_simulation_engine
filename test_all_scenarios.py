import asyncio
import json
import httpx
import websockets

SCENARIOS_AND_EXPECTED_ALERTS = [
    ("fall", "fall_suspected", 25),
    ("hypoxia", "spo2_low", 20),
    ("tachycardia", "hr_high", 20),
    ("bradycardia", "hr_low", 20),
    ("fever", "temp_high", 20),
    ("wander", "geofence_exit", 20),
    ("hypertensive", "bp_high", 10),
    ("hypoglycemia", "glucose_low", 10),
]

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=35.0) as client:
        # 1. Login
        res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
        assert res.status_code == 200
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        res = await client.get("/me", headers=headers)
        budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
        print(f"Testing All Scenarios on Pak Budi ({budi_id})...")

        ws_url = f"ws://127.0.0.1:8000/ws/{budi_id}?token={token}"
        async with websockets.connect(ws_url) as ws:
            print("Connected to WebSocket room!")

            async def wait_for_alert(expected_type: str, timeout: float = 22.0):
                end_time = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < end_time:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        msg = json.loads(raw)
                        if msg.get("type") == "alert":
                            alert_type = msg["data"].get("type")
                            print(f"  [WS Alert Received] type={alert_type}, severity={msg['data'].get('severity')}")
                            if alert_type == expected_type:
                                return msg["data"]
                    except asyncio.TimeoutError:
                        continue
                raise TimeoutError(f"Did not receive alert {expected_type} within {timeout}s")

            # Reset to normal first
            await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)
            await asyncio.sleep(2)

            # Test each scenario
            for scenario, expected_alert, dur in SCENARIOS_AND_EXPECTED_ALERTS:
                print(f"\n--> Triggering Scenario: '{scenario}' (expects '{expected_alert}')...")
                res = await client.post(
                    f"/sim/{budi_id}/trigger",
                    json={"scenario": scenario, "duration_s": dur},
                    headers=headers
                )
                assert res.status_code == 200, res.text
                alert_data = await wait_for_alert(expected_alert, timeout=float(dur + 5))
                print(f"  SUCCESS: Scenario '{scenario}' correctly emitted '{expected_alert}'!")

                # Reset to normal between tests
                await client.post(f"/sim/{budi_id}/trigger", json={"scenario": "normal", "duration_s": 1}, headers=headers)
                await asyncio.sleep(2)

            print("\nAll 8 active continuous & measurement scenarios verified successfully over WebSocket!")

if __name__ == "__main__":
    asyncio.run(main())
