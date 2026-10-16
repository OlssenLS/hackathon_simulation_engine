import asyncio
import math
import random
import uuid
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, Tuple, List

from sqlalchemy import select, insert
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import User, ElderProfile, Vital, Location, Measurement
from app.ws import manager
from app.services.anomaly import anomaly_engine

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")


class Simulator:
    def __init__(self):
        # Per-elder state: elder_id -> state dict
        self.states: Dict[str, Dict[str, Any]] = {}
        self.running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.last_periodic_slot: Dict[str, str] = {}  # elder_id -> "YYYY-MM-DD-HH"

    def get_active_elders_count(self) -> int:
        return len(self.states)

    def init_elder_state(self, elder: ElderProfile, user: User, now: datetime) -> Dict[str, Any]:
        has_hypertension = "Hipertensi" in (elder.conditions or [])
        hr_offset = 4.0 if has_hypertension else 0.0
        base_sys = 132.0 if has_hypertension else 122.0
        base_dia = 84.0 if has_hypertension else 78.0

        local_dt = now.astimezone(JAKARTA_TZ)
        local_hour = local_dt.hour
        if 22 <= local_hour or local_hour < 5:
            activity = "sleeping"
            duration = random.randint(30, 90)
        elif 5 <= local_hour < 7:
            activity = "walking" if random.random() < 0.3 else "resting"
            duration = random.randint(5, 20)
        else:
            activity = "resting"
            duration = random.randint(10, 45)

        return {
            "elder_id": elder.user_id,
            "name": user.name,
            "conditions": elder.conditions or [],
            "hr_offset": hr_offset,
            "home_lat": elder.home_lat,
            "home_lng": elder.home_lng,
            "geofence_m": elder.geofence_m,
            "sim_enabled": elder.sim_enabled,
            "cur_lat": elder.home_lat,
            "cur_lng": elder.home_lng,
            "activity": activity,
            "activity_until": now + timedelta(minutes=duration),
            "current_hr": 70.0 + hr_offset,
            "hr_drift": 0.0,
            "current_sys_bp": base_sys,
            "current_dia_bp": base_dia,
            "bp_counter": 0,
            "steps_today": 0,
            "last_day": local_dt.date(),
            "scenario": "normal",
            "scenario_until": now,
            "scenario_tick": 0,
            "ramp_back_until": now,
            "last_tick": now,
        }

    async def trigger_scenario(self, elder_id: str, scenario: str, duration_s: int = 30):
        now = datetime.now(timezone.utc)
        if elder_id not in self.states:
            # Try to load elder
            async with AsyncSessionLocal() as db:
                stmt = select(ElderProfile, User).join(User, ElderProfile.user_id == User.id).where(ElderProfile.user_id == elder_id)
                res = (await db.execute(stmt)).first()
                if res:
                    p, u = res
                    self.states[elder_id] = self.init_elder_state(p, u, now)

        state = self.states.get(elder_id)
        if not state:
            return None

        old_scenario = state.get("scenario", "normal")
        if scenario == "normal":
            state["scenario"] = "normal"
            state["scenario_tick"] = 0
            state["scenario_until"] = now
            if old_scenario != "normal":
                await manager.broadcast_to_elder(
                    elder_id=elder_id,
                    event_type="scenario",
                    data={"scenario": old_scenario, "active": False},
                )
            return {"scenario": "normal", "activity": state["activity"], "duration_remaining_s": 0}

        # Active scenario setup
        state["scenario"] = scenario
        state["scenario_tick"] = 0
        state["scenario_until"] = now + timedelta(seconds=duration_s)

        # Notify WS
        await manager.broadcast_to_elder(
            elder_id=elder_id,
            event_type="scenario",
            data={"scenario": scenario, "active": True},
        )

        # Immediate effects for measurement scenarios
        if scenario == "hypertensive":
            meas_id = str(uuid.uuid4())
            bp_val1, bp_val2 = 192.0, 124.0
            async with AsyncSessionLocal() as db:
                m = Measurement(
                    id=meas_id,
                    elder_id=elder_id,
                    ts=now,
                    kind="bp",
                    value1=bp_val1,
                    value2=bp_val2,
                    context=None,
                    source="sim",
                )
                db.add(m)
                await db.commit()

            await anomaly_engine.process_measurement(
                elder_id=elder_id,
                elder_name=state["name"],
                kind="bp",
                value1=bp_val1,
                value2=bp_val2,
                context=None,
                now=now,
            )
            await manager.broadcast_to_elder(
                elder_id=elder_id,
                event_type="measurement",
                data={"kind": "bp", "value1": bp_val1, "value2": bp_val2, "context": None},
            )

        elif scenario == "hypoglycemia":
            meas_id = str(uuid.uuid4())
            g_val = 48.0
            async with AsyncSessionLocal() as db:
                m = Measurement(
                    id=meas_id,
                    elder_id=elder_id,
                    ts=now,
                    kind="glucose",
                    value1=g_val,
                    value2=None,
                    context="random",
                    source="sim",
                )
                db.add(m)
                await db.commit()

            await anomaly_engine.process_measurement(
                elder_id=elder_id,
                elder_name=state["name"],
                kind="glucose",
                value1=g_val,
                value2=None,
                context="random",
                now=now,
            )
            await manager.broadcast_to_elder(
                elder_id=elder_id,
                event_type="measurement",
                data={"kind": "glucose", "value1": g_val, "value2": None, "context": "random"},
            )

        return {
            "scenario": scenario,
            "activity": state["activity"],
            "duration_remaining_s": duration_s,
        }

    def get_status(self, elder_id: str) -> Optional[Dict[str, Any]]:
        state = self.states.get(elder_id)
        if not state:
            return None
        now = datetime.now(timezone.utc)
        remaining = max(0, int((state["scenario_until"] - now).total_seconds())) if state["scenario"] != "normal" else 0
        return {
            "scenario": state["scenario"] if remaining > 0 else "normal",
            "activity": state["activity"],
            "duration_remaining_s": remaining,
        }

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                # Log error and continue
                print("Simulator error in tick:", e)
            await asyncio.sleep(settings.SIM_TICK_SECONDS)

    async def _tick(self):
        now = datetime.now(timezone.utc)
        local_dt = now.astimezone(JAKARTA_TZ)
        local_date = local_dt.date()
        local_hour = local_dt.hour
        local_minute = local_dt.minute

        # Reload or sync elders if empty
        if not self.states:
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(ElderProfile, User)
                    .join(User, ElderProfile.user_id == User.id)
                    .where(ElderProfile.sim_enabled == True)
                )
                results = (await db.execute(stmt)).all()
                for p, u in results:
                    self.states[p.user_id] = self.init_elder_state(p, u, now)

        vitals_to_insert = []
        locations_to_insert = []
        measurements_to_insert = []

        for elder_id, state in list(self.states.items()):
            if not state.get("sim_enabled", True):
                continue

            # Midnight reset for steps
            if state["last_day"] != local_date:
                state["last_day"] = local_date
                state["steps_today"] = 0

            scenario = state.get("scenario", "normal")
            scenario_tick = state.get("scenario_tick", 0)

            # Check scenario expiration
            if scenario != "normal" and now > state["scenario_until"]:
                old_scenario = scenario
                state["scenario"] = "normal"
                state["scenario_tick"] = 0
                scenario = "normal"
                asyncio.create_task(
                    manager.broadcast_to_elder(
                        elder_id=elder_id,
                        event_type="scenario",
                        data={"scenario": old_scenario, "active": False},
                    )
                )

            # Activity state machine (if not forced by scenario)
            if scenario == "fall":
                state["activity"] = "resting"
            elif scenario == "inactivity":
                state["activity"] = "resting"
            elif now > state["activity_until"]:
                # Transition
                if 22 <= local_hour or local_hour < 5:
                    next_act = "sleeping" if random.random() < 0.95 else "resting"
                    duration_min = random.randint(30, 90)
                elif 5 <= local_hour < 7:
                    next_act = "walking" if random.random() < 0.35 else "resting"
                    duration_min = random.randint(5, 20)
                elif 7 <= local_hour < 21:
                    r = random.random()
                    if r < 0.03:
                        next_act = "exercising"
                        duration_min = random.randint(5, 15)
                    elif r < 0.23:
                        next_act = "walking"
                        duration_min = random.randint(2, 10)
                    else:
                        next_act = "resting"
                        duration_min = random.randint(15, 60)
                else:
                    next_act = "sleeping" if random.random() < 0.20 else "resting"
                    duration_min = random.randint(20, 60)

                state["activity"] = next_act
                state["activity_until"] = now + timedelta(minutes=duration_min)

            current_activity = state["activity"]
            hr_offset = state["hr_offset"]

            # Calculate metrics
            base_accel = 1.0
            accel_noise = 0.02
            if current_activity == "sleeping":
                base_hr = 56.0 + hr_offset
                base_spo2 = 96.5
                base_resp = 13.0
                base_accel = 1.0
                accel_noise = 0.02
            elif current_activity == "resting":
                base_hr = 70.0 + hr_offset
                base_spo2 = 97.0
                base_resp = 16.0
                base_accel = 1.0
                accel_noise = 0.02
            elif current_activity == "walking":
                base_hr = 92.0 + hr_offset
                base_spo2 = 97.2
                base_resp = 19.0
                base_accel = 1.15
                accel_noise = 0.15
                if scenario != "inactivity":
                    state["steps_today"] += random.randint(4, 6)  # ~1.5 to 2 steps/s over 3s
            else:  # exercising
                base_hr = 112.0 + hr_offset
                base_spo2 = 97.5
                base_resp = 24.0
                base_accel = 1.4
                accel_noise = 0.3
                if scenario != "inactivity":
                    state["steps_today"] += random.randint(7, 9)  # ~2.5/s over 3s

            # Scenario overrides
            target_hr = base_hr
            target_spo2 = base_spo2
            target_temp = 36.4 + 0.25 * math.sin((local_hour - 8) * math.pi / 12.0)
            target_resp = base_resp
            cur_accel = random.gauss(base_accel, accel_noise)

            if scenario == "fall":
                if scenario_tick == 0:
                    cur_accel = 3.4  # Impact spike
                else:
                    cur_accel = random.uniform(0.05, 0.1)  # Immobile
                    target_hr = base_hr + 25.0
                state["scenario_tick"] += 1

            elif scenario == "tachycardia":
                target_hr = 135.0
            elif scenario == "bradycardia":
                target_hr = 38.0
            elif scenario == "hypoxia":
                target_spo2 = 87.0
            elif scenario == "fever":
                target_temp = 38.6
            elif scenario == "inactivity":
                # steps don't increase, hr flat
                target_hr = 68.0

            # Smooth HR with physiological beat-to-beat variability (RSA jitter)
            state["hr_drift"] += random.uniform(-0.6, 0.6)
            state["hr_drift"] = max(-6.0, min(6.0, state["hr_drift"]))
            instant_jitter = random.uniform(-2.5, 2.5)
            target_hr_with_drift = target_hr + state["hr_drift"] + instant_jitter
            state["current_hr"] = state["current_hr"] + 0.45 * (target_hr_with_drift - state["current_hr"])
            hr_val = round(max(30.0, min(190.0, state["current_hr"])), 1)

            # SpO2
            spo2_val = round(max(80.0, min(99.0, target_spo2 + random.gauss(0, 0.4))), 1)

            # Temp
            temp_val = round(target_temp + random.gauss(0, 0.04), 2)

            # Resp rate
            resp_val = round(max(6.0, min(36.0, target_resp + random.gauss(0, 0.6))), 1)
            accel_val = round(max(0.02, min(4.0, cur_accel)), 2)

            # GPS update
            home_lat = state["home_lat"]
            home_lng = state["home_lng"]
            cur_lat = state["cur_lat"]
            cur_lng = state["cur_lng"]

            if scenario == "wander":
                # Drifts steadily away from home
                cur_lat += 0.0004
                cur_lng += 0.0004
            elif current_activity == "walking":
                # Random walk with return bias beyond 80m
                dist_lat = (cur_lat - home_lat) * 111320
                dist_lng = (cur_lng - home_lng) * 111320 * math.cos(home_lat * math.pi / 180)
                dist_m = math.sqrt(dist_lat**2 + dist_lng**2)

                step_lat = random.uniform(-0.000015, 0.000015)
                step_lng = random.uniform(-0.000015, 0.000015)

                if dist_m > 80.0:
                    # Bias toward home
                    step_lat -= 0.00001 * (cur_lat - home_lat) / max(0.00001, abs(cur_lat - home_lat))
                    step_lng -= 0.00001 * (cur_lng - home_lng) / max(0.00001, abs(cur_lng - home_lng))

                cur_lat += step_lat
                cur_lng += step_lng

            state["cur_lat"] = cur_lat
            state["cur_lng"] = cur_lng

            # Distance from home
            dlat = (cur_lat - home_lat) * 111320
            dlng = (cur_lng - home_lng) * 111320 * math.cos(home_lat * math.pi / 180)
            dist_from_home = round(math.sqrt(dlat**2 + dlng**2), 1)
            inside_geofence = dist_from_home <= state["geofence_m"]

            vitals_reading = {
                "elder_id": elder_id,
                "ts": now,
                "hr": hr_val,
                "spo2": spo2_val,
                "steps": state["steps_today"],
                "temp": temp_val,
                "resp_rate": resp_val,
                "accel_mag": accel_val,
                "activity": current_activity,
            }
            vitals_to_insert.append(vitals_reading)

            loc_reading = {
                "elder_id": elder_id,
                "ts": now,
                "lat": round(cur_lat, 6),
                "lng": round(cur_lng, 6),
            }
            locations_to_insert.append(loc_reading)

            # WS broadcast data objects
            ts_str = now.isoformat().replace("+00:00", "Z")
            vitals_ws_data = {
                "ts": ts_str,
                "hr": hr_val,
                "spo2": spo2_val,
                "steps": state["steps_today"],
                "temp": temp_val,
                "resp_rate": resp_val,
                "accel_mag": accel_val,
                "activity": current_activity,
            }
            location_ws_data = {
                "lat": round(cur_lat, 6),
                "lng": round(cur_lng, 6),
                "distance_from_home_m": dist_from_home,
                "inside_geofence": inside_geofence,
            }

            # Continuous Ambulatory Blood Pressure (BP)
            has_hypertension = "Hipertensi" in state.get("conditions", [])
            base_sys = 132.0 if has_hypertension else 122.0
            base_dia = 84.0 if has_hypertension else 78.0

            if scenario == "hypertensive":
                target_sys = 190.0
                target_dia = 122.0
            elif scenario == "tachycardia":
                target_sys = base_sys + 12.0
                target_dia = base_dia + 6.0
            elif scenario in ("fall", "bradycardia"):
                target_sys = base_sys - 18.0
                target_dia = base_dia - 12.0
            else:
                target_sys = base_sys
                target_dia = base_dia

            if "current_sys_bp" not in state:
                state["current_sys_bp"] = target_sys
                state["current_dia_bp"] = target_dia

            # Ambulatory fluctuation: physiological drift + small pulse noise (+- 1.5 mmHg)
            state["current_sys_bp"] += 0.35 * (target_sys - state["current_sys_bp"]) + random.uniform(-1.5, 1.5)
            state["current_dia_bp"] += 0.35 * (target_dia - state["current_dia_bp"]) + random.uniform(-1.0, 1.0)
            sys_bp = round(max(70.0, min(220.0, state["current_sys_bp"])))
            dia_bp = round(max(40.0, min(140.0, state["current_dia_bp"])))

            # Broadcast BP reading via WebSocket on every tick
            asyncio.create_task(
                manager.broadcast_to_elder(
                    elder_id=elder_id,
                    event_type="measurement",
                    data={"kind": "bp", "value1": float(sys_bp), "value2": float(dia_bp), "context": None},
                    ts=ts_str,
                )
            )

            # Persist BP reading to database periodically (every 10 ticks / ~20s) or during hypertensive crisis
            state["bp_counter"] = state.get("bp_counter", 0) + 1
            if state["bp_counter"] % 10 == 0 or scenario == "hypertensive":
                m_id = str(uuid.uuid4())
                measurements_to_insert.append({
                    "id": m_id,
                    "elder_id": elder_id,
                    "ts": now,
                    "kind": "bp",
                    "value1": float(sys_bp),
                    "value2": float(dia_bp),
                    "context": None,
                    "source": "sim",
                })

            # Periodic scheduled measurements check (e.g. Glucose)
            slot_key = f"{local_date.isoformat()}-{local_hour}"
            last_slot = self.last_periodic_slot.get(elder_id)
            if last_slot != slot_key:
                self.last_periodic_slot[elder_id] = slot_key
                has_diabetes = "Diabetes" in state["conditions"] or "Diabetes tipe 2" in state["conditions"]

                # Glucose at 07:00 (fasting), 13:00 (postprandial), 19:00 (postprandial)
                if local_hour in (7, 13, 19):
                    ctx = "fasting" if local_hour == 7 else "postprandial"
                    if local_hour == 7:
                        g_val = round(random.gauss(118, 6) if has_diabetes else random.gauss(92, 4))
                    else:
                        g_val = round(random.gauss(165, 8) if has_diabetes else random.gauss(128, 6))

                    m_id = str(uuid.uuid4())
                    measurements_to_insert.append({
                        "id": m_id,
                        "elder_id": elder_id,
                        "ts": now,
                        "kind": "glucose",
                        "value1": float(g_val),
                        "value2": None,
                        "context": ctx,
                        "source": "sim",
                    })
                    asyncio.create_task(
                        manager.broadcast_to_elder(
                            elder_id=elder_id,
                            event_type="measurement",
                            data={"kind": "glucose", "value1": float(g_val), "value2": None, "context": ctx},
                            ts=ts_str,
                        )
                    )
                    asyncio.create_task(
                        anomaly_engine.process_measurement(
                            elder_id=elder_id,
                            elder_name=state["name"],
                            kind="glucose",
                            value1=float(g_val),
                            value2=None,
                            context=ctx,
                            now=now,
                        )
                    )

            # Broadcast vitals and location
            asyncio.create_task(
                manager.broadcast_to_elder(
                    elder_id=elder_id,
                    event_type="vitals",
                    data=vitals_ws_data,
                    ts=ts_str,
                )
            )
            asyncio.create_task(
                manager.broadcast_to_elder(
                    elder_id=elder_id,
                    event_type="location",
                    data=location_ws_data,
                    ts=ts_str,
                )
            )

            # Run anomaly detection
            asyncio.create_task(
                anomaly_engine.process_tick(
                    elder_id=elder_id,
                    elder_name=state["name"],
                    vitals=vitals_ws_data,
                    location=location_ws_data,
                    geofence_m=state["geofence_m"],
                    home_coords=(home_lat, home_lng),
                    now=now,
                )
            )

        # Bulk insert tick data
        if vitals_to_insert:
            async with AsyncSessionLocal() as db:
                await db.execute(insert(Vital).values(vitals_to_insert))
                if locations_to_insert:
                    await db.execute(insert(Location).values(locations_to_insert))
                if measurements_to_insert:
                    await db.execute(insert(Measurement).values(measurements_to_insert))
                await db.commit()


simulator = Simulator()
