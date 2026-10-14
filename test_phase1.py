import asyncio
import httpx
from app.main import app
from app.db import engine, Base

async def run_phase1_test():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Health check
        res = await client.get("/health")
        print("Health status:", res.status_code, res.json())
        assert res.status_code == 200

        # 2. Register caregiver
        cg_data = {
            "name": "Test Caregiver",
            "email": "test_cg@test.com",
            "password": "password123",
            "role": "caregiver",
            "phone": "+628123456789"
        }
        res = await client.post("/auth/register", json=cg_data)
        print("Register Caregiver:", res.status_code)
        assert res.status_code in (201, 400) # 400 if already exists

        # 3. Login caregiver
        res = await client.post("/auth/login", json={"email": "test_cg@test.com", "password": "password123"})
        print("Login Caregiver:", res.status_code)
        assert res.status_code == 200
        cg_token = res.json()["access_token"]

        # 4. GET /me for caregiver
        res = await client.get("/me", headers={"Authorization": f"Bearer {cg_token}"})
        print("Me Caregiver:", res.status_code, res.json())
        assert res.status_code == 200
        assert res.json()["user"]["role"] == "caregiver"

        # 5. Register elder
        elder_data = {
            "name": "Test Elder",
            "email": "test_elder@test.com",
            "password": "password123",
            "role": "elder",
            "phone": "+628987654321",
            "birth_date": "1954-05-10"
        }
        res = await client.post("/auth/register", json=elder_data)
        print("Register Elder:", res.status_code)
        assert res.status_code in (201, 400)

        # 6. Login elder
        res = await client.post("/auth/login", json={"email": "test_elder@test.com", "password": "password123"})
        print("Login Elder:", res.status_code)
        assert res.status_code == 200
        elder_token = res.json()["access_token"]

        # 7. GET /me for elder
        res = await client.get("/me", headers={"Authorization": f"Bearer {elder_token}"})
        print("Me Elder:", res.status_code, res.json())
        assert res.status_code == 200
        assert res.json()["user"]["role"] == "elder"
        assert res.json()["elder"]["link_code"] is not None

        print("=== PHASE 1 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(run_phase1_test())
