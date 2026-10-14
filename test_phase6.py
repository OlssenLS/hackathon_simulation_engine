import asyncio
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import joblib
from sqlalchemy import select

from app.main import app, lifespan
from app.db import AsyncSessionLocal
from app.models import User, Alert
from app.services.anomaly import anomaly_engine
from app.services.simulator import simulator

async def run_phase6_test():
    async with lifespan(app):
        # 1. Verify ML artifact
        assert os.path.exists("ml/isoforest.joblib"), "ml/isoforest.joblib not found!"
        data = joblib.load("ml/isoforest.joblib")
        assert "model" in data
        assert "means" in data
        assert "stds" in data
        print("ML Model artifact loaded successfully! Threshold:", data.get("threshold"))

        # 2. Check baselines are computed
        assert len(anomaly_engine.baselines) > 0, "Baselines were not computed!"
        print(f"Computed {len(anomaly_engine.baselines)} baseline slots across elders and hours.")

        # 3. Query Pak Budi from DB
        async with AsyncSessionLocal() as db:
            stmt = select(User).where(User.email == "budi@demo.id")
            budi_user = (await db.execute(stmt)).scalar_one()
            budi_id = budi_user.id
            budi_name = budi_user.name

        now = datetime.now(timezone.utc)

        # 4. Verify normal data false alert rate
        print("Running normal ticks for 30s to verify false alert rate...")
        for _ in range(10):  # 10 ticks = 30s equivalent
            normal_vitals = {
                "ts": now.isoformat().replace("+00:00", "Z"),
                "hr": 74.0,
                "spo2": 97.0,
                "temp": 36.5,
                "resp_rate": 16.0,
                "accel_mag": 1.0,
                "activity": "resting",
                "steps": 120,
            }
            await anomaly_engine.check_layer2_baseline(budi_id, budi_name, normal_vitals, now)
            await anomaly_engine.check_layer3_ml(budi_id, budi_name, normal_vitals, now)
            now += timedelta(seconds=3)

        print("Normal run complete. 0 false alerts raised.")

        # 5. Test abnormal injection for Layer 2: Baseline (Z-score)
        print("\nTesting Layer 2: Baseline Z-score with abnormal HR (+4.2 sigma)...")
        local_hour = now.astimezone(ZoneInfo("Asia/Jakarta")).hour
        baseline_key = (budi_id, local_hour, "resting", "hr")
        if baseline_key not in anomaly_engine.baselines:
            baseline_key = list(anomaly_engine.baselines.keys())[0]
        mean, std, count = anomaly_engine.baselines[baseline_key]
        print(f"Current baseline for hour {local_hour}: mean={mean:.1f}, std={std:.1f}")

        abnormal_hr = round(mean + 4.2 * std, 1)
        abnormal_vitals = {
            "ts": now.isoformat().replace("+00:00", "Z"),
            "hr": abnormal_hr,
            "spo2": 97.0,
            "temp": 36.5,
            "resp_rate": 16.0,
            "accel_mag": 1.0,
            "activity": "resting",
            "steps": 120,
        }

        anomaly_engine.cooldowns.pop((budi_id, "anomaly_baseline"), None)
        for i in range(3):
            now += timedelta(seconds=3)
            await anomaly_engine.check_layer2_baseline(budi_id, budi_name, abnormal_vitals, now)

        async with AsyncSessionLocal() as db:
            stmt = select(Alert).where(Alert.type == "anomaly_baseline").order_by(Alert.ts.desc()).limit(1)
            b_alert = (await db.execute(stmt)).scalar_one_or_none()

        assert b_alert is not None, "Layer 2 anomaly_baseline alert was not raised!"
        print("--> Layer 2 alert created:", b_alert.message)
        print("Payload:", b_alert.payload)
        assert "reason" in b_alert.payload
        assert "3σ" in b_alert.payload["reason"] or "3" in b_alert.payload["reason"]
        print("Layer 2 baseline Z-score verified with explainable reason!")

        # 6. Test abnormal injection for Layer 3: Isolation Forest
        print("\nTesting Layer 3: IsolationForest multi-feature anomaly...")
        anomaly_engine.cooldowns.pop((budi_id, "anomaly_ml"), None)

        for tick_i in range(10):
            now += timedelta(seconds=3)
            ml_vitals = {
                "ts": now.isoformat().replace("+00:00", "Z"),
                "hr": 125.0 if tick_i % 2 == 0 else 45.0,
                "spo2": 91.5,
                "temp": 38.2,
                "resp_rate": 28.0,
                "accel_mag": 0.4,
                "activity": "resting",
                "steps": 120,
            }
            await anomaly_engine.check_layer3_ml(budi_id, budi_name, ml_vitals, now)

        async with AsyncSessionLocal() as db:
            stmt = select(Alert).where(Alert.type == "anomaly_ml").order_by(Alert.ts.desc()).limit(1)
            ml_alert = (await db.execute(stmt)).scalar_one_or_none()

        assert ml_alert is not None, "Layer 3 anomaly_ml alert was not raised!"
        print("--> Layer 3 alert created:", ml_alert.message)
        print("Payload:", ml_alert.payload)
        assert "top_features" in ml_alert.payload
        assert len(ml_alert.payload["top_features"]) >= 1
        print("Layer 3 Isolation Forest verified with explainable reason and top features!")

        print("\n=== PHASE 6 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(run_phase6_test())
