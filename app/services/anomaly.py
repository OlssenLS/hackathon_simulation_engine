import os
import math
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
from zoneinfo import ZoneInfo
import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import Alert, User, ElderProfile, Vital
from app.ws import manager
from app import thresholds

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
ML_MODEL_PATH = "ml/isoforest.joblib"


class AnomalyEngine:
    def __init__(self):
        # Cooldown per (elder_id, alert_type) -> (timestamp, severity)
        self.cooldowns: Dict[Tuple[str, str], Tuple[datetime, str]] = {}
        # Consecutiveness tracking: (elder_id, condition_key) -> count
        self.consecutive_counts: Dict[Tuple[str, str], int] = {}
        # Fall tracking per elder_id
        self.fall_trackers: Dict[str, Dict[str, Any]] = {}
        # Inactivity tracking per elder_id
        self.inactivity_trackers: Dict[str, Dict[str, Any]] = {}

        # Layer 2: Baseline cache
        # (elder_id, local_hour, activity, metric) -> (mean, std, count)
        self.baselines: Dict[Tuple[str, int, str, str], Tuple[float, float, int]] = {}
        self.last_baseline_compute: Optional[datetime] = None

        # Layer 3: Isolation Forest
        self.ml_model: Optional[IsolationForest] = None
        self.ml_threshold: float = -0.15
        self.ml_feature_means: np.ndarray = np.zeros(6)
        self.ml_feature_stds: np.ndarray = np.ones(6)
        self.feature_names = ["hr", "spo2", "temp", "steps_delta_5m", "hr_std_5m", "accel_mean_1m"]
        # Rolling recent ticks per elder for feature extraction: elder_id -> list of tick dicts
        self.tick_history: Dict[str, List[Dict[str, Any]]] = {}

    def should_trigger(self, elder_id: str, alert_type: str, severity: str, now: datetime) -> bool:
        key = (elder_id, alert_type)
        if key in self.cooldowns:
            last_ts, last_sev = self.cooldowns[key]
            # Escalation from warning/info to critical bypasses cooldown
            if last_sev != "critical" and severity == "critical":
                self.cooldowns[key] = (now, severity)
                return True
            # Cooldown is 2 minutes (120 seconds)
            if (now - last_ts).total_seconds() < 120:
                return False

        self.cooldowns[key] = (now, severity)
        return True

    def check_consecutive(self, elder_id: str, condition_key: str, is_active: bool, required_ticks: int = 2) -> bool:
        key = (elder_id, condition_key)
        if is_active:
            count = self.consecutive_counts.get(key, 0) + 1
            self.consecutive_counts[key] = count
            return count >= required_ticks
        else:
            self.consecutive_counts[key] = 0
            return False

    async def raise_alert(
        self,
        elder_id: str,
        alert_type: str,
        severity: str,
        message: str,
        payload: Optional[Dict[str, Any]],
        source: str,
        now: datetime,
    ) -> Optional[Dict[str, Any]]:
        if not self.should_trigger(elder_id, alert_type, severity, now):
            return None

        alert_id = str(uuid.uuid4())
        alert_data = {
            "id": alert_id,
            "elder_id": elder_id,
            "type": alert_type,
            "severity": severity,
            "message": message,
            "payload": payload or {},
            "source": source,
            "ts": now.isoformat().replace("+00:00", "Z"),
            "acknowledged": False,
            "acknowledged_at": None,
        }

        # Persist alert
        async with AsyncSessionLocal() as db:
            new_alert = Alert(
                id=alert_id,
                elder_id=elder_id,
                type=alert_type,
                severity=severity,
                message=message,
                payload=payload or {},
                source=source,
                ts=now,
                acknowledged=False,
                acknowledged_at=None,
            )
            db.add(new_alert)
            await db.commit()

        # WS broadcast
        await manager.broadcast_to_elder(
            elder_id=elder_id,
            event_type="alert",
            data=alert_data,
            ts=alert_data["ts"],
        )

        return alert_data

    # ==================== Layer 2: Baseline (Z-Score) ====================

    async def recompute_baselines(self):
        """Recomputes mean and std dev per elder, per local hour, per activity for last 7 days."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)

        async with AsyncSessionLocal() as db:
            stmt = select(Vital).where(Vital.ts >= since).order_by(Vital.ts.asc())
            vitals = (await db.execute(stmt)).scalars().all()

        groups: Dict[Tuple[str, int, str], Dict[str, List[float]]] = {}
        for v in vitals:
            local_hour = v.ts.astimezone(JAKARTA_TZ).hour
            key = (v.elder_id, local_hour, v.activity)
            if key not in groups:
                groups[key] = {"hr": [], "spo2": [], "temp": []}
            groups[key]["hr"].append(v.hr)
            groups[key]["spo2"].append(v.spo2)
            groups[key]["temp"].append(v.temp)

        new_baselines = {}
        for (e_id, l_hr, act), metric_data in groups.items():
            for m_name in ["hr", "spo2", "temp"]:
                vals = metric_data[m_name]
                if len(vals) >= 5:
                    mean_val = float(np.mean(vals))
                    std_val = float(np.std(vals))
                    new_baselines[(e_id, l_hr, act, m_name)] = (mean_val, std_val, len(vals))

        self.baselines = new_baselines
        self.last_baseline_compute = now

    async def check_layer2_baseline(
        self,
        elder_id: str,
        elder_name: str,
        vitals: Dict[str, Any],
        now: datetime,
    ):
        local_hour = now.astimezone(JAKARTA_TZ).hour
        activity = vitals["activity"]
        hr = vitals["hr"]

        # Check HR baseline
        base_key = (elder_id, local_hour, activity, "hr")
        baseline = self.baselines.get(base_key)
        if not baseline:
            # Fallback across activities if sparse
            base_key_any = (elder_id, local_hour, "resting", "hr")
            baseline = self.baselines.get(base_key_any)

        if baseline:
            mean, std, count = baseline
            if std > 1.0 and count >= 10:
                z = (hr - mean) / std
                is_z_outlier = abs(z) > 3.0
                is_fired = self.check_consecutive(elder_id, "baseline_hr", is_z_outlier, 3)
                if is_fired:
                    direction = "naik" if z > 0 else "turun"
                    reason_msg = f"HR {int(hr)} bpm {direction} lebih dari 3 sigma dari kebiasaan pukul {local_hour:02d}:00 (rata-rata {int(mean)})"
                    await self.raise_alert(
                        elder_id=elder_id,
                        alert_type="anomaly_baseline",
                        severity="warning",
                        message=f"Pola detak jantung tidak biasa pada {elder_name}. {reason_msg}.",
                        payload={
                            "metric": "hr",
                            "value": hr,
                            "baseline_mean": round(mean, 1),
                            "baseline_std": round(std, 1),
                            "z": round(z, 2),
                            "reason": reason_msg,
                        },
                        source="baseline",
                        now=now,
                    )

    # ==================== Layer 3: Isolation Forest ====================

    async def train_or_load_isolation_forest(self):
        """Loads trained IsolationForest from ml/isoforest.joblib or trains a new one."""
        if os.path.exists(ML_MODEL_PATH):
            try:
                data = joblib.load(ML_MODEL_PATH)
                self.ml_model = data["model"]
                self.ml_feature_means = data["means"]
                self.ml_feature_stds = data["stds"]
                self.ml_threshold = data.get("threshold", -0.15)
                print(f"Loaded IsolationForest model from {ML_MODEL_PATH} (threshold: {self.ml_threshold})")
                return
            except Exception as e:
                print("Failed to load existing ML model, retraining:", e)

        print("Training IsolationForest on seeded 7-day normal history...")
        async with AsyncSessionLocal() as db:
            stmt = select(Vital).order_by(Vital.ts.asc())
            vitals = (await db.execute(stmt)).scalars().all()

        if len(vitals) < 200:
            print("Not enough vitals data to train IsolationForest.")
            return

        # Extract features for training:
        # [hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m]
        # Since backfill has 1-minute resolution, 5m window = 5 samples
        features_list = []
        for i in range(5, len(vitals)):
            v = vitals[i]
            window_5 = vitals[i-5:i+1]
            hr = v.hr
            spo2 = v.spo2
            temp = v.temp
            steps_delta_5m = max(0, v.steps - window_5[0].steps)
            hr_std_5m = float(np.std([w.hr for w in window_5]))
            accel_mean_1m = float(np.mean([w.accel_mag for w in window_5[-2:]]))

            features_list.append([hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m])

        X = np.array(features_list)
        means = np.mean(X, axis=0)
        stds = np.std(X, axis=0)
        stds[stds == 0] = 1.0

        model = IsolationForest(
            contamination=0.01,
            n_estimators=100,
            random_state=42
        )
        model.fit(X)

        scores = model.decision_function(X)
        # Choose a safe threshold so normal data rarely triggers (< 0.2 percentile)
        threshold = float(np.percentile(scores, 0.2))

        os.makedirs(os.path.dirname(ML_MODEL_PATH), exist_ok=True)
        joblib.dump({
            "model": model,
            "means": means,
            "stds": stds,
            "threshold": threshold,
        }, ML_MODEL_PATH)

        self.ml_model = model
        self.ml_feature_means = means
        self.ml_feature_stds = stds
        self.ml_threshold = threshold
        print(f"IsolationForest trained and saved to {ML_MODEL_PATH}. Threshold: {threshold:.4f}")

    async def check_layer3_ml(
        self,
        elder_id: str,
        elder_name: str,
        vitals: Dict[str, Any],
        now: datetime,
    ):
        if self.ml_model is None:
            return

        # Maintain rolling tick history (last 5 minutes = 100 ticks of 3s)
        history = self.tick_history.get(elder_id, [])
        history.append({
            "ts": now,
            "hr": vitals["hr"],
            "spo2": vitals["spo2"],
            "temp": vitals["temp"],
            "steps": vitals["steps"],
            "accel": vitals["accel_mag"],
        })
        # Keep last 100 ticks
        if len(history) > 100:
            history = history[-100:]
        self.tick_history[elder_id] = history

        # Need at least 5 ticks to compute rolling stats
        if len(history) < 5:
            return

        # Compute features
        hr = vitals["hr"]
        spo2 = vitals["spo2"]
        temp = vitals["temp"]
        steps_delta_5m = max(0, vitals["steps"] - history[0]["steps"])
        hr_std_5m = float(np.std([h["hr"] for h in history]))
        # Last 1 min = last 20 ticks
        recent_accels = [h["accel"] for h in history[-20:]]
        accel_mean_1m = float(np.mean(recent_accels))

        feat_vector = np.array([[hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m]])
        score = float(self.ml_model.decision_function(feat_vector)[0])

        is_ml_anomaly = score < self.ml_threshold
        is_fired = self.check_consecutive(elder_id, "ml_anomaly", is_ml_anomaly, 3)

        if is_fired:
            # Top 2 features with greatest absolute deviation in std units
            devs = np.abs((feat_vector[0] - self.ml_feature_means) / self.ml_feature_stds)
            top_indices = np.argsort(devs)[-2:][::-1]
            top_features = [
                {
                    "feature": self.feature_names[idx],
                    "value": round(float(feat_vector[0][idx]), 2),
                    "deviation_sigma": round(float(devs[idx]), 2),
                }
                for idx in top_indices
            ]
            top_names = ", ".join([f["feature"] for f in top_features])
            reason_msg = f"Deteksi anomali pola multi-variabel (skor {score:.2f}). Penyimpangan terbesar pada: {top_names}."

            await self.raise_alert(
                elder_id=elder_id,
                alert_type="anomaly_ml",
                severity="warning",
                message=f"Pola vitalitas {elder_name} menunjukkan anomali statistik. {reason_msg}",
                payload={
                    "score": round(score, 4),
                    "threshold": round(self.ml_threshold, 4),
                    "top_features": top_features,
                    "reason": reason_msg,
                },
                source="ml",
                now=now,
            )

    # ==================== Main Tick Processing ====================

    async def process_tick(
        self,
        elder_id: str,
        elder_name: str,
        vitals: Dict[str, Any],
        location: Dict[str, Any],
        geofence_m: float,
        home_coords: Tuple[float, float],
        now: datetime,
    ):
        hr = vitals["hr"]
        spo2 = vitals["spo2"]
        temp = vitals["temp"]
        resp_rate = vitals["resp_rate"]
        accel_mag = vitals["accel_mag"]
        activity = vitals["activity"]
        steps = vitals["steps"]

        # ================= SpO2 =================
        spo2_crit = spo2 < thresholds.SPO2_CRITICAL_LOW
        spo2_warn = spo2 < thresholds.SPO2_WARNING_LOW
        is_spo2_crit = self.check_consecutive(elder_id, "spo2_crit", spo2_crit, 2)
        is_spo2_warn = self.check_consecutive(elder_id, "spo2_warn", spo2_warn, 2)

        if is_spo2_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="spo2_low",
                severity="critical",
                message=f"Kadar oksigen sangat rendah (SpO2 {spo2}%). Segera berikan bantuan!",
                payload={"spo2": spo2},
                source="rule",
                now=now,
            )
        elif is_spo2_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="spo2_low",
                severity="warning",
                message=f"Kadar oksigen rendah (SpO2 {spo2}%). Periksa kondisi segera.",
                payload={"spo2": spo2},
                source="rule",
                now=now,
            )

        # ================= Heart Rate =================
        if activity == "exercising":
            hr_high_crit = hr > thresholds.HR_EXERCISING_CRITICAL_HIGH
            hr_high_warn = hr > thresholds.HR_EXERCISING_WARNING_HIGH
            hr_low_crit = hr < thresholds.HR_RESTING_CRITICAL_LOW
            hr_low_warn = hr < thresholds.HR_RESTING_WARNING_LOW
        else:
            hr_high_crit = hr > thresholds.HR_RESTING_CRITICAL_HIGH
            hr_high_warn = hr > thresholds.HR_RESTING_WARNING_HIGH
            hr_low_crit = hr < thresholds.HR_RESTING_CRITICAL_LOW
            hr_low_warn = hr < thresholds.HR_RESTING_WARNING_LOW

        is_hr_high_crit = self.check_consecutive(elder_id, "hr_high_crit", hr_high_crit, 2)
        is_hr_high_warn = self.check_consecutive(elder_id, "hr_high_warn", hr_high_warn, 2)
        is_hr_low_crit = self.check_consecutive(elder_id, "hr_low_crit", hr_low_crit, 2)
        is_hr_low_warn = self.check_consecutive(elder_id, "hr_low_warn", hr_low_warn, 2)

        if is_hr_high_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="hr_high",
                severity="critical",
                message=f"Detak jantung sangat tinggi ({hr} bpm). Segera periksa {elder_name}!",
                payload={"hr": hr, "activity": activity},
                source="rule",
                now=now,
            )
        elif is_hr_high_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="hr_high",
                severity="warning",
                message=f"Detak jantung tinggi ({hr} bpm). Segera istirahatkan {elder_name}.",
                payload={"hr": hr, "activity": activity},
                source="rule",
                now=now,
            )
        elif is_hr_low_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="hr_low",
                severity="critical",
                message=f"Detak jantung sangat rendah ({hr} bpm). Periksa kondisi darurat {elder_name}!",
                payload={"hr": hr, "activity": activity},
                source="rule",
                now=now,
            )
        elif is_hr_low_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="hr_low",
                severity="warning",
                message=f"Detak jantung rendah ({hr} bpm). Periksa kondisi {elder_name}.",
                payload={"hr": hr, "activity": activity},
                source="rule",
                now=now,
            )

        # ================= Temperature =================
        temp_high_crit = temp >= thresholds.TEMP_CRITICAL_HIGH
        temp_high_warn = temp >= thresholds.TEMP_WARNING_HIGH
        temp_low_crit = temp < thresholds.TEMP_CRITICAL_LOW
        temp_low_warn = temp < thresholds.TEMP_WARNING_LOW

        is_temp_high_crit = self.check_consecutive(elder_id, "temp_high_crit", temp_high_crit, 2)
        is_temp_high_warn = self.check_consecutive(elder_id, "temp_high_warn", temp_high_warn, 2)
        is_temp_low_crit = self.check_consecutive(elder_id, "temp_low_crit", temp_low_crit, 2)
        is_temp_low_warn = self.check_consecutive(elder_id, "temp_low_warn", temp_low_warn, 2)

        if is_temp_high_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="temp_high",
                severity="critical",
                message=f"Suhu tubuh sangat tinggi ({temp}°C). Potensi demam tinggi!",
                payload={"temp": temp},
                source="rule",
                now=now,
            )
        elif is_temp_high_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="temp_high",
                severity="warning",
                message=f"Suhu tubuh tinggi ({temp}°C). Periksa kemungkinan demam {elder_name}.",
                payload={"temp": temp},
                source="rule",
                now=now,
            )
        elif is_temp_low_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="temp_low",
                severity="critical",
                message=f"Suhu tubuh sangat rendah ({temp}°C). Potensi hipotermia!",
                payload={"temp": temp},
                source="rule",
                now=now,
            )
        elif is_temp_low_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="temp_low",
                severity="warning",
                message=f"Suhu tubuh rendah ({temp}°C).",
                payload={"temp": temp},
                source="rule",
                now=now,
            )

        # ================= Respiratory Rate =================
        resp_crit = resp_rate > thresholds.RESP_CRITICAL_HIGH or resp_rate < thresholds.RESP_CRITICAL_LOW
        resp_warn = resp_rate > thresholds.RESP_WARNING_HIGH or resp_rate < thresholds.RESP_WARNING_LOW
        is_resp_crit = self.check_consecutive(elder_id, "resp_crit", resp_crit, 2)
        is_resp_warn = self.check_consecutive(elder_id, "resp_warn", resp_warn, 2)

        if is_resp_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="resp_abnormal",
                severity="critical",
                message=f"Laju pernapasan kritis ({resp_rate} napas/menit)!",
                payload={"resp_rate": resp_rate},
                source="rule",
                now=now,
            )
        elif is_resp_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="resp_abnormal",
                severity="warning",
                message=f"Laju pernapasan tidak normal ({resp_rate} napas/menit).",
                payload={"resp_rate": resp_rate},
                source="rule",
                now=now,
            )

        # ================= Fall Detection =================
        fall_info = self.fall_trackers.get(elder_id)
        if accel_mag > 2.8:
            self.fall_trackers[elder_id] = {
                "peak_g": accel_mag,
                "spike_ts": now,
                "window_accels": [],
            }
        elif fall_info is not None:
            time_since_spike = (now - fall_info["spike_ts"]).total_seconds()
            if time_since_spike <= 20.0:
                fall_info["window_accels"].append(accel_mag)
                if len(fall_info["window_accels"]) >= 2:
                    mean_accel = sum(fall_info["window_accels"]) / len(fall_info["window_accels"])
                    if mean_accel < 0.25 and activity != "sleeping":
                        del self.fall_trackers[elder_id]
                        await self.raise_alert(
                            elder_id=elder_id,
                            alert_type="fall_suspected",
                            severity="critical",
                            message=f"Kemungkinan jatuh terdeteksi. Segera periksa kondisi {elder_name}.",
                            payload={
                                "peak_g": fall_info["peak_g"],
                                "mean_accel_post": round(mean_accel, 2),
                                "location": location,
                                "latest_hr": hr,
                            },
                            source="sos",
                            now=now,
                        )
            else:
                del self.fall_trackers[elder_id]

        # ================= Geofence =================
        dist = location["distance_from_home_m"]
        geo_crit = dist > (3.0 * geofence_m)
        geo_warn = dist > geofence_m

        is_geo_crit = self.check_consecutive(elder_id, "geo_crit", geo_crit, 2)
        is_geo_warn = self.check_consecutive(elder_id, "geo_warn", geo_warn, 2)

        if is_geo_crit:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="geofence_exit",
                severity="critical",
                message=f"{elder_name} berada sangat jauh di luar area aman (±{int(dist)} m dari rumah)!",
                payload={"distance_from_home_m": dist, "lat": location["lat"], "lng": location["lng"]},
                source="rule",
                now=now,
            )
        elif is_geo_warn:
            await self.raise_alert(
                elder_id=elder_id,
                alert_type="geofence_exit",
                severity="warning",
                message=f"{elder_name} keluar dari area aman (±{int(dist)} m dari rumah).",
                payload={"distance_from_home_m": dist, "lat": location["lat"], "lng": location["lng"]},
                source="rule",
                now=now,
            )

        # ================= Inactivity =================
        local_hour = now.astimezone(JAKARTA_TZ).hour
        is_awake_hours = 7 <= local_hour < 21
        inactivity_limit_min = settings.INACTIVITY_MIN

        inact = self.inactivity_trackers.get(elder_id)
        if inact is None:
            self.inactivity_trackers[elder_id] = {
                "last_steps": steps,
                "resting_since": now if activity == "resting" else None,
            }
        else:
            if steps > inact["last_steps"] or activity != "resting":
                inact["last_steps"] = steps
                inact["resting_since"] = None
            else:
                if inact["resting_since"] is None:
                    inact["resting_since"] = now
                elif is_awake_hours:
                    resting_mins = (now - inact["resting_since"]).total_seconds() / 60.0
                    if resting_mins >= inactivity_limit_min:
                        await self.raise_alert(
                            elder_id=elder_id,
                            alert_type="inactivity",
                            severity="warning",
                            message=f"Tidak ada aktivitas terdeteksi selama lebih dari {int(resting_mins)} menit pada jam aktif.",
                            payload={"resting_minutes": int(resting_mins), "steps": steps},
                            source="rule",
                            now=now,
                        )

        # ================= Layer 2: Baseline Check =================
        # Recompute baselines every 30 minutes if needed
        if self.last_baseline_compute is None or (now - self.last_baseline_compute).total_seconds() > 1800:
            await self.recompute_baselines()

        await self.check_layer2_baseline(elder_id, elder_name, vitals, now)

        # ================= Layer 3: Isolation Forest Check =================
        await self.check_layer3_ml(elder_id, elder_name, vitals, now)

    async def process_measurement(
        self,
        elder_id: str,
        elder_name: str,
        kind: str,
        value1: float,
        value2: Optional[float],
        context: Optional[str],
        now: datetime,
    ):
        if kind == "bp":
            sys_bp = value1
            dia_bp = value2 or 0.0
            crit = (
                sys_bp >= thresholds.BP_SYS_CRITICAL_HIGH
                or sys_bp < thresholds.BP_SYS_CRITICAL_LOW
                or dia_bp >= thresholds.BP_DIA_CRITICAL_HIGH
            )
            warn_high = sys_bp >= thresholds.BP_SYS_WARNING_HIGH or dia_bp >= thresholds.BP_DIA_WARNING_HIGH
            warn_low = sys_bp < thresholds.BP_SYS_WARNING_LOW or dia_bp < thresholds.BP_DIA_WARNING_LOW

            if crit:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="bp_high" if sys_bp >= 140 else "bp_low",
                    severity="critical",
                    message=f"Tekanan darah sangat abnormal: {int(sys_bp)}/{int(dia_bp)} mmHg!",
                    payload={"systolic": sys_bp, "diastolic": dia_bp},
                    source="rule",
                    now=now,
                )
            elif warn_high:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="bp_high",
                    severity="warning",
                    message=f"Tekanan darah tinggi: {int(sys_bp)}/{int(dia_bp)} mmHg.",
                    payload={"systolic": sys_bp, "diastolic": dia_bp},
                    source="rule",
                    now=now,
                )
            elif warn_low:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="bp_low",
                    severity="warning",
                    message=f"Tekanan darah rendah: {int(sys_bp)}/{int(dia_bp)} mmHg.",
                    payload={"systolic": sys_bp, "diastolic": dia_bp},
                    source="rule",
                    now=now,
                )

        elif kind == "glucose":
            g_val = value1
            crit = g_val > thresholds.GLUCOSE_CRITICAL_HIGH or g_val < thresholds.GLUCOSE_CRITICAL_LOW
            warn_high = (
                (context == "fasting" and g_val > thresholds.GLUCOSE_FASTING_WARNING_HIGH)
                or (context != "fasting" and g_val > thresholds.GLUCOSE_POSTPRANDIAL_WARNING_HIGH)
            )
            warn_low = g_val < thresholds.GLUCOSE_WARNING_LOW

            if crit:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="glucose_high" if g_val > 200 else "glucose_low",
                    severity="critical",
                    message=f"Gula darah sangat abnormal ({int(g_val)} mg/dL). Segera hubungi bantuan medis!",
                    payload={"glucose": g_val, "context": context},
                    source="rule",
                    now=now,
                )
            elif warn_low:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="glucose_low",
                    severity="warning",
                    message=f"Gula darah rendah ({int(g_val)} mg/dL). Berikan asupan gula dan hubungi dokter.",
                    payload={"glucose": g_val, "context": context},
                    source="rule",
                    now=now,
                )
            elif warn_high:
                await self.raise_alert(
                    elder_id=elder_id,
                    alert_type="glucose_high",
                    severity="warning",
                    message=f"Kadar gula darah tinggi ({int(g_val)} mg/dL).",
                    payload={"glucose": g_val, "context": context},
                    source="rule",
                    now=now,
                )


anomaly_engine = AnomalyEngine()
