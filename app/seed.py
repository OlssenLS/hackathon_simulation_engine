import uuid
import random
import math
from datetime import datetime, timezone, timedelta, date, time
from zoneinfo import ZoneInfo
from typing import List, Dict, Any

from sqlalchemy import select, func, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import (
    User,
    ElderProfile,
    Vital,
    Measurement,
    Location,
    Medication,
    MedLog,
    DoctorAppt,
    Document,
    Alert,
)
from app.security import get_password_hash

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")


def get_circadian_activity(local_hour: int, current_activity: str) -> str:
    r = random.random()
    if 22 <= local_hour or local_hour < 5:
        # Sleeping 95%, resting 5%
        return "sleeping" if r < 0.95 else "resting"
    elif 5 <= local_hour < 7:
        # Mix of resting/walking
        return "walking" if r < 0.35 else "resting"
    elif 7 <= local_hour < 21:
        # Mostly resting, 20% walking, 3% exercising
        if current_activity == "walking" and r < 0.6:
            return "walking"  # keep walking for a bit
        if current_activity == "exercising" and r < 0.3:
            return "exercising"
        if r < 0.03:
            return "exercising"
        elif r < 0.23:
            return "walking"
        return "resting"
    else:  # 21:00 to 22:00
        return "sleeping" if r < 0.20 else "resting"


async def generate_backfill_for_elder(
    db: AsyncSession,
    elder_id: str,
    name: str,
    conditions: List[str],
    home_lat: float,
    home_lng: float,
    medications: List[Medication],
    start_dt: datetime,
    end_dt: datetime,
):
    """Generates 1 sample/minute backfill between start_dt and end_dt."""
    has_hypertension = "Hipertensi" in conditions
    has_diabetes = "Diabetes" in conditions or "Diabetes tipe 2" in conditions

    hr_offset = 4.0 if has_hypertension else 0.0
    current_activity = "resting"
    hr_drift = 0.0
    current_hr = 70.0 + hr_offset

    vitals_batch: List[Dict[str, Any]] = []
    locations_batch: List[Dict[str, Any]] = []
    measurements_batch: List[Dict[str, Any]] = []

    # Position tracking
    cur_lat = home_lat
    cur_lng = home_lng

    # Steps tracking
    last_day = None
    steps_today = 0

    total_minutes = int((end_dt - start_dt).total_seconds() // 60)
    current_dt = start_dt

    for minute_idx in range(total_minutes):
        current_dt += timedelta(minutes=1)
        local_dt = current_dt.astimezone(JAKARTA_TZ)
        local_date = local_dt.date()
        local_hour = local_dt.hour
        local_minute = local_dt.minute

        # Reset steps at local midnight
        if last_day != local_date:
            last_day = local_date
            steps_today = 0

        # Update activity occasionally or every 3-5 minutes
        if minute_idx % 3 == 0:
            current_activity = get_circadian_activity(local_hour, current_activity)

        # Base HR
        if current_activity == "sleeping":
            target_hr = 56.0 + hr_offset
            target_spo2 = 96.5
            target_resp = 13.0
            accel = random.gauss(1.0, 0.02)
        elif current_activity == "resting":
            target_hr = 70.0 + hr_offset
            target_spo2 = 97.0
            target_resp = 16.0
            accel = random.gauss(1.0, 0.02)
        elif current_activity == "walking":
            target_hr = 92.0 + hr_offset
            target_spo2 = 97.2
            target_resp = 19.0
            accel = random.gauss(1.15, 0.15)
            steps_today += random.randint(90, 120)
            # Random walk drift up to ~60 m from home
            d_lat = random.uniform(-0.0001, 0.0001)
            d_lng = random.uniform(-0.0001, 0.0001)
            cur_lat = max(home_lat - 0.0006, min(home_lat + 0.0006, cur_lat + d_lat))
            cur_lng = max(home_lng - 0.0006, min(home_lng + 0.0006, cur_lng + d_lng))
        else:  # exercising
            target_hr = 112.0 + hr_offset
            target_spo2 = 97.5
            target_resp = 24.0
            accel = random.gauss(1.4, 0.3)
            steps_today += random.randint(140, 170)

        # HR drift and smoothing
        hr_drift += random.uniform(-0.4, 0.4)
        hr_drift = max(-6.0, min(6.0, hr_drift))
        target_hr_drifted = target_hr + hr_drift + random.gauss(0, 1.5)
        current_hr = current_hr + 0.3 * (target_hr_drifted - current_hr)
        hr_val = round(max(45.0, min(140.0, current_hr)), 1)

        # SpO2
        spo2_val = round(max(94.0, min(99.0, target_spo2 + random.gauss(0, 0.5))), 1)

        # Temp: circadian ±0.3 around 36.4
        circadian_temp = 0.25 * math.sin((local_hour - 8) * math.pi / 12.0)
        temp_val = round(36.4 + circadian_temp + random.gauss(0, 0.05), 2)

        # Resp rate
        resp_val = round(max(10.0, min(28.0, target_resp + random.gauss(0, 0.8))), 1)
        accel_val = round(max(0.7, min(2.5, accel)), 2)

        vitals_batch.append({
            "elder_id": elder_id,
            "ts": current_dt,
            "hr": hr_val,
            "spo2": spo2_val,
            "steps": steps_today,
            "temp": temp_val,
            "resp_rate": resp_val,
            "accel_mag": accel_val,
            "activity": current_activity,
        })

        # Location: 1 point every 5 minutes in backfill to keep DB clean and fast
        if minute_idx % 5 == 0:
            locations_batch.append({
                "elder_id": elder_id,
                "ts": current_dt,
                "lat": round(cur_lat, 6),
                "lng": round(cur_lng, 6),
            })

        # Scheduled periodic measurements
        # BP at 08:00 and 20:00
        if local_minute == 0 and local_hour in (8, 20):
            if has_hypertension:
                sys_bp = round(random.gauss(138, 5))
                dia_bp = round(random.gauss(86, 4))
            else:
                sys_bp = round(random.gauss(120, 4))
                dia_bp = round(random.gauss(78, 3))
            measurements_batch.append({
                "id": str(uuid.uuid4()),
                "elder_id": elder_id,
                "ts": current_dt,
                "kind": "bp",
                "value1": float(sys_bp),
                "value2": float(dia_bp),
                "context": None,
                "source": "sim",
            })

        # Glucose at 07:00 (fasting), 13:00 (postprandial), 19:00 (postprandial)
        if local_minute == 0 and local_hour in (7, 13, 19):
            if local_hour == 7:
                ctx = "fasting"
                g_val = round(random.gauss(118, 7)) if has_diabetes else round(random.gauss(92, 5))
            else:
                ctx = "postprandial"
                g_val = round(random.gauss(165, 10)) if has_diabetes else round(random.gauss(128, 7))
            measurements_batch.append({
                "id": str(uuid.uuid4()),
                "elder_id": elder_id,
                "ts": current_dt,
                "kind": "glucose",
                "value1": float(g_val),
                "value2": None,
                "context": ctx,
                "source": "sim",
            })

    # Bulk insert in chunks of 2000
    CHUNK_SIZE = 2000
    for i in range(0, len(vitals_batch), CHUNK_SIZE):
        await db.execute(insert(Vital).values(vitals_batch[i:i + CHUNK_SIZE]))

    for i in range(0, len(locations_batch), CHUNK_SIZE):
        await db.execute(insert(Location).values(locations_batch[i:i + CHUNK_SIZE]))

    if measurements_batch:
        await db.execute(insert(Measurement).values(measurements_batch))

    # Medication logs for the 7 days
    # ~85% taken, 10% late (taken), 5% missed
    med_logs_batch = []
    days_back = int((end_dt - start_dt).days) + 1
    start_date = (start_dt.astimezone(JAKARTA_TZ)).date()

    for d_offset in range(days_back):
        d = start_date + timedelta(days=d_offset)
        weekday = d.weekday()
        for med in medications:
            if med.days and weekday not in med.days:
                continue
            for t_str in med.times:
                hh, mm = map(int, t_str.split(":"))
                sched_local = datetime.combine(d, time(hh, mm), tzinfo=JAKARTA_TZ)
                sched_utc = sched_local.astimezone(timezone.utc)
                if sched_utc < start_dt or sched_utc > end_dt:
                    continue

                roll = random.random()
                if roll < 0.85:
                    status = "taken"
                    conf_at = sched_utc + timedelta(minutes=random.randint(5, 20))
                elif roll < 0.95:
                    status = "taken"
                    conf_at = sched_utc + timedelta(minutes=random.randint(35, 60))
                else:
                    status = "missed"
                    conf_at = None

                med_logs_batch.append({
                    "id": str(uuid.uuid4()),
                    "medication_id": med.id,
                    "elder_id": elder_id,
                    "scheduled_at": sched_utc,
                    "status": status,
                    "confirmed_at": conf_at,
                })

    if med_logs_batch:
        await db.execute(insert(MedLog).values(med_logs_batch))

    # Historical alerts (2 to 3 mild alerts)
    historical_alerts = [
        {
            "id": str(uuid.uuid4()),
            "elder_id": elder_id,
            "type": "bp_high",
            "severity": "warning",
            "message": f"Tekanan darah {name} agak tinggi: 144/92 mmHg.",
            "payload": {"systolic": 144, "diastolic": 92},
            "source": "rule",
            "ts": end_dt - timedelta(days=3, hours=4),
            "acknowledged": True,
            "acknowledged_at": end_dt - timedelta(days=3, hours=3, minutes=45),
        },
        {
            "id": str(uuid.uuid4()),
            "elder_id": elder_id,
            "type": "geofence_exit",
            "severity": "warning",
            "message": f"{name} keluar dari area aman (±180 m dari rumah).",
            "payload": {"distance_from_home_m": 180.0},
            "source": "rule",
            "ts": end_dt - timedelta(days=5, hours=2),
            "acknowledged": True,
            "acknowledged_at": end_dt - timedelta(days=5, hours=1, minutes=50),
        },
        {
            "id": str(uuid.uuid4()),
            "elder_id": elder_id,
            "type": "med_missed",
            "severity": "warning",
            "message": f"Jadwal obat {medications[0].name} terlewat.",
            "payload": {"medication_name": medications[0].name},
            "source": "rule",
            "ts": end_dt - timedelta(days=1, hours=8),
            "acknowledged": True,
            "acknowledged_at": end_dt - timedelta(days=1, hours=7, minutes=30),
        },
    ]
    await db.execute(insert(Alert).values(historical_alerts))
    await db.commit()


async def seed_database_if_empty():
    async with AsyncSessionLocal() as db:
        user_count = await db.scalar(select(func.count(User.id)))
        if user_count and user_count > 0:
            print("Database already contains users. Skipping seed.")
            return

        print("Seeding database with demo accounts and 7-day backfill...")
        pwd_hash = get_password_hash("demo1234")

        # 1. Caregiver
        caregiver = User(
            name="Rina (anak)",
            email="caregiver@demo.id",
            password_hash=pwd_hash,
            role="caregiver",
            phone="+6281200000001",
        )
        db.add(caregiver)
        await db.flush()

        # 2. Pak Budi (Elder 1)
        budi_user = User(
            name="Pak Budi",
            email="budi@demo.id",
            password_hash=pwd_hash,
            role="elder",
            phone="+6281200000002",
        )
        db.add(budi_user)
        await db.flush()

        budi_profile = ElderProfile(
            user_id=budi_user.id,
            birth_date=date(1952, 5, 14),
            conditions=["Hipertensi"],
            link_code="BUDI23",
            caregiver_id=caregiver.id,
            doctor_name="dr. Bambang, Sp.PD",
            home_lat=-6.2349,
            home_lng=106.9896,
            geofence_m=150.0,
            sim_enabled=True,
        )
        db.add(budi_profile)

        # Pak Budi medications
        budi_meds = [
            Medication(
                elder_id=budi_user.id,
                name="Amlodipin 5 mg",
                dose="5 mg",
                times=["08:00"],
                days=[],
                notes="Minum setelah makan pagi",
                active=True,
            ),
            Medication(
                elder_id=budi_user.id,
                name="Candesartan 8 mg",
                dose="8 mg",
                times=["20:00"],
                days=[],
                notes="Minum malam sebelum tidur",
                active=True,
            ),
        ]
        db.add_all(budi_meds)

        # Pak Budi doctor appts
        now = datetime.now(timezone.utc)
        budi_appts = [
            DoctorAppt(
                elder_id=budi_user.id,
                doctor="dr. Bambang, Sp.PD",
                location="RS Mitra Keluarga Bekasi",
                ts=now + timedelta(days=3, hours=10),
                notes="Pemeriksaan tensi rutin dan evaluasi obat hipertensi",
            ),
            DoctorAppt(
                elder_id=budi_user.id,
                doctor="dr. Hendra, Sp.JP",
                location="Klinik Jantung Sehat",
                ts=now + timedelta(days=14, hours=9),
                notes="Pemeriksaan EKG berkala",
            ),
        ]
        db.add_all(budi_appts)

        # Pak Budi placeholder doc
        budi_doc = Document(
            elder_id=budi_user.id,
            title="Hasil Lab Darah Terakhir",
            file_url="https://placeholder.test/lab_budi.pdf",
            mime="application/pdf",
            size_bytes=1048576,
        )
        db.add(budi_doc)

        # 3. Bu Sari (Elder 2)
        sari_user = User(
            name="Bu Sari",
            email="sari@demo.id",
            password_hash=pwd_hash,
            role="elder",
            phone="+6281200000003",
        )
        db.add(sari_user)
        await db.flush()

        sari_profile = ElderProfile(
            user_id=sari_user.id,
            birth_date=date(1956, 8, 22),
            conditions=["Diabetes tipe 2"],
            link_code="SARI45",
            caregiver_id=caregiver.id,
            doctor_name="dr. Siti, Sp.PD-KEMD",
            home_lat=-6.2380,
            home_lng=106.9920,
            geofence_m=150.0,
            sim_enabled=True,
        )
        db.add(sari_profile)

        # Bu Sari medications
        sari_meds = [
            Medication(
                elder_id=sari_user.id,
                name="Metformin 500 mg",
                dose="500 mg",
                times=["07:30", "19:30"],
                days=[],
                notes="Minum bersama atau sesudah makan",
                active=True,
            ),
            Medication(
                elder_id=sari_user.id,
                name="Vitamin D 1000 IU",
                dose="1000 IU",
                times=["09:00"],
                days=[],
                notes="Suplemen harian vitamin D",
                active=True,
            ),
        ]
        db.add_all(sari_meds)

        # Bu Sari doctor appts
        sari_appts = [
            DoctorAppt(
                elder_id=sari_user.id,
                doctor="dr. Siti, Sp.PD-KEMD",
                location="RS Hermina Bekasi",
                ts=now + timedelta(days=5, hours=11),
                notes="Cek HbA1c dan konsultasi gizi diabetes",
            ),
            DoctorAppt(
                elder_id=sari_user.id,
                doctor="dr. Anita, Sp.M",
                location="Klinik Mata Nusantara",
                ts=now + timedelta(days=20, hours=14),
                notes="Pemeriksaan retina mata tahunan",
            ),
        ]
        db.add_all(sari_appts)

        # Bu Sari placeholder doc
        sari_doc = Document(
            elder_id=sari_user.id,
            title="Resume Medis Klinik",
            file_url="https://placeholder.test/resume_sari.pdf",
            mime="application/pdf",
            size_bytes=524288,
        )
        db.add(sari_doc)

        await db.commit()

        # 7-day backfill for Pak Budi
        start_time = now - timedelta(days=7)
        print("Generating 7-day backfill for Pak Budi...")
        await generate_backfill_for_elder(
            db=db,
            elder_id=budi_user.id,
            name="Pak Budi",
            conditions=["Hipertensi"],
            home_lat=-6.2349,
            home_lng=106.9896,
            medications=budi_meds,
            start_dt=start_time,
            end_dt=now,
        )

        # 7-day backfill for Bu Sari
        print("Generating 7-day backfill for Bu Sari...")
        await generate_backfill_for_elder(
            db=db,
            elder_id=sari_user.id,
            name="Bu Sari",
            conditions=["Diabetes tipe 2"],
            home_lat=-6.2380,
            home_lng=106.9920,
            medications=sari_meds,
            start_dt=start_time,
            end_dt=now,
        )

        print("Database seeded successfully with demo accounts and 7-day history!")


async def backfill_downtime_gap_if_needed():
    """On startup after downtime: backfill the gap from last_ts to now at 1 sample/minute (cap at 24 h) so charts have no holes."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ElderProfile, User)
            .join(User, ElderProfile.user_id == User.id)
            .where(ElderProfile.sim_enabled == True)
        )
        elders = (await db.execute(stmt)).all()

        for profile, user in elders:
            last_ts_stmt = select(func.max(Vital.ts)).where(Vital.elder_id == profile.user_id)
            last_ts = await db.scalar(last_ts_stmt)

            if last_ts and (now - last_ts) > timedelta(minutes=2):
                gap = now - last_ts
                backfill_duration = min(gap, timedelta(hours=24))
                start_gap = now - backfill_duration
                print(f"Backfilling downtime gap of {backfill_duration} for {user.name} from {start_gap} to {now}...")

                meds_stmt = select(Medication).where(and_(Medication.elder_id == profile.user_id, Medication.active == True))
                meds = (await db.execute(meds_stmt)).scalars().all()

                await generate_backfill_for_elder(
                    db=db,
                    elder_id=profile.user_id,
                    name=user.name,
                    conditions=profile.conditions or [],
                    home_lat=profile.home_lat,
                    home_lng=profile.home_lng,
                    medications=meds,
                    start_dt=start_gap,
                    end_dt=now,
                )

