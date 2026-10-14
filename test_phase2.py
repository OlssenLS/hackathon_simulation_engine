import asyncio
import httpx
from app.main import app, lifespan

async def run_phase2_test():
    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # 1. Login as Caregiver
            res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
            print("Caregiver login:", res.status_code)
            assert res.status_code == 200, res.text
            cg_token = res.json()["access_token"]
            cg_headers = {"Authorization": f"Bearer {cg_token}"}

            # 2. Check /me for Caregiver (should have 2 linked elders: Pak Budi & Bu Sari)
            res = await client.get("/me", headers=cg_headers)
            print("Caregiver /me:", res.status_code)
            assert res.status_code == 200
            me_data = res.json()
            assert len(me_data["elders"]) == 2
            budi_id = next(e["id"] for e in me_data["elders"] if "Budi" in e["name"])
            sari_id = next(e["id"] for e in me_data["elders"] if "Sari" in e["name"])
            print(f"Found Pak Budi ID: {budi_id}, Bu Sari ID: {sari_id}")

            # 3. GET /elders
            res = await client.get("/elders", headers=cg_headers)
            print("GET /elders:", res.status_code, len(res.json()))
            assert res.status_code == 200
            assert len(res.json()) == 2

            # 4. GET /elders/{id}
            res = await client.get(f"/elders/{budi_id}", headers=cg_headers)
            print(f"GET /elders/{budi_id}:", res.status_code, res.json()["name"])
            assert res.status_code == 200
            assert res.json()["link_code"] == "BUDI23"

            # 5. GET /elders/{id}/caregiver
            res = await client.get(f"/elders/{budi_id}/caregiver", headers=cg_headers)
            print("GET caregiver contact:", res.status_code, res.json())
            assert res.status_code == 200
            assert res.json()["name"] == "Rina (anak)"

            # 6. Acceptance Check: /elders/{id}/vitals?interval=1h returns 7 days of points (~168 points)
            res = await client.get(f"/elders/{budi_id}/vitals?interval=1h", headers=cg_headers)
            print("GET vitals 1h interval status:", res.status_code)
            assert res.status_code == 200
            vitals = res.json()
            print(f"Returned {len(vitals)} downsampled points for 7 days")
            # 7 days * 24 hours = ~168 points
            assert len(vitals) >= 150, f"Expected ~168 points, got {len(vitals)}"
            print("Sample vitals point:", vitals[0])

            # 7. Check Elder login directly
            res = await client.post("/auth/login", json={"email": "budi@demo.id", "password": "demo1234"})
            print("Budi login:", res.status_code)
            assert res.status_code == 200
            budi_token = res.json()["access_token"]
            budi_headers = {"Authorization": f"Bearer {budi_token}"}

            res = await client.get("/me", headers=budi_headers)
            print("Budi /me:", res.status_code, res.json()["elder"])
            assert res.status_code == 200
            assert res.json()["elder"]["link_code"] == "BUDI23"

            print("=== PHASE 2 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(run_phase2_test())
