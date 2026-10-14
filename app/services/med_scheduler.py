import asyncio
import uuid
from datetime import datetime, timezone, timedelta, time, date
from zoneinfo import ZoneInfo
from typing import Set, Optional
from sqlalchemy import select, and_, insert, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import Medication, MedLog, ElderProfile, User
from app.ws import manager
from app.services.anomaly import anomaly_engine

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")


class MedScheduler:
    def __init__(self):
        self.running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.emitted_due_log_ids: Set[str] = set()
        self.grace_minutes: int = getattr(settings, "MED_GRACE_MINUTES", 30)

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

    async def ensure_today_logs_for_med(self, med: Medication, db):
        now = datetime.now(timezone.utc)
        local_now = now.astimezone(JAKARTA_TZ)
        today = local_now.date()
        weekday = today.weekday()

        if med.days and weekday not in med.days:
            return

        for t_str in med.times:
            try:
                hh, mm = map(int, t_str.split(":"))
                sched_local = datetime.combine(today, time(hh, mm), tzinfo=JAKARTA_TZ)
                sched_utc = sched_local.astimezone(timezone.utc)

                # Check if log already exists
                stmt = select(MedLog).where(
                    and_(
                        MedLog.medication_id == med.id,
                        MedLog.scheduled_at == sched_utc
                    )
                )
                existing = (await db.execute(stmt)).scalar_one_or_none()
                if not existing:
                    new_log = MedLog(
                        id=str(uuid.uuid4()),
                        medication_id=med.id,
                        elder_id=med.elder_id,
                        scheduled_at=sched_utc,
                        status="pending",
                        confirmed_at=None,
                    )
                    db.add(new_log)
            except Exception as e:
                print(f"Error scheduling med {med.id} at {t_str}: {e}")

        await db.commit()

    async def _run_loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                print("MedScheduler tick error:", e)
            await asyncio.sleep(5)  # Tick every 5 seconds for responsive due/missed checks

    async def _tick(self):
        now = datetime.now(timezone.utc)
        local_now = now.astimezone(JAKARTA_TZ)
        today = local_now.date()
        weekday = today.weekday()

        async with AsyncSessionLocal() as db:
            # 1. Fetch all active medications
            stmt = (
                select(Medication)
                .join(ElderProfile, Medication.elder_id == ElderProfile.user_id)
                .where(Medication.active == True)
            )
            meds = (await db.execute(stmt)).scalars().all()

            # Ensure today's logs exist
            for med in meds:
                if med.days and weekday not in med.days:
                    continue
                for t_str in med.times:
                    try:
                        hh, mm = map(int, t_str.split(":"))
                        sched_local = datetime.combine(today, time(hh, mm), tzinfo=JAKARTA_TZ)
                        sched_utc = sched_local.astimezone(timezone.utc)

                        stmt_chk = select(MedLog).where(
                            and_(
                                MedLog.medication_id == med.id,
                                MedLog.scheduled_at == sched_utc
                            )
                        )
                        existing = (await db.execute(stmt_chk)).scalar_one_or_none()
                        if not existing:
                            new_log = MedLog(
                                id=str(uuid.uuid4()),
                                medication_id=med.id,
                                elder_id=med.elder_id,
                                scheduled_at=sched_utc,
                                status="pending",
                                confirmed_at=None,
                            )
                            db.add(new_log)
                            await db.commit()
                    except Exception:
                        pass

            # 2. Check pending logs
            # Query pending logs for today (or past days)
            pending_stmt = (
                select(MedLog, Medication, User)
                .join(Medication, MedLog.medication_id == Medication.id)
                .join(User, MedLog.elder_id == User.id)
                .where(MedLog.status == "pending")
            )
            results = (await db.execute(pending_stmt)).all()

            for log, med, elder_user in results:
                sched_at = log.scheduled_at
                # Due check: now >= scheduled_at
                if now >= sched_at and log.id not in self.emitted_due_log_ids:
                    self.emitted_due_log_ids.add(log.id)
                    ts_str = now.isoformat().replace("+00:00", "Z")
                    sched_str = sched_at.isoformat().replace("+00:00", "Z")
                    await manager.broadcast_to_elder(
                        elder_id=log.elder_id,
                        event_type="med",
                        data={
                            "log_id": log.id,
                            "name": med.name,
                            "dose": med.dose,
                            "status": "pending",
                            "scheduled_at": sched_str,
                        },
                        ts=ts_str,
                    )

                # Missed check: now >= scheduled_at + grace_minutes
                grace_td = timedelta(minutes=self.grace_minutes)
                if now >= (sched_at + grace_td):
                    log.status = "missed"
                    await db.commit()

                    ts_str = now.isoformat().replace("+00:00", "Z")
                    sched_str = sched_at.isoformat().replace("+00:00", "Z")

                    # Raise alert
                    await anomaly_engine.raise_alert(
                        elder_id=log.elder_id,
                        alert_type="med_missed",
                        severity="warning",
                        message=f"Jadwal minum obat {med.name} ({med.dose}) terlewat.",
                        payload={
                            "medication_id": med.id,
                            "medication_name": med.name,
                            "dose": med.dose,
                            "scheduled_at": sched_str,
                        },
                        source="rule",
                        now=now,
                    )

                    # Emit med WS event
                    await manager.broadcast_to_elder(
                        elder_id=log.elder_id,
                        event_type="med",
                        data={
                            "log_id": log.id,
                            "name": med.name,
                            "dose": med.dose,
                            "status": "missed",
                            "scheduled_at": sched_str,
                        },
                        ts=ts_str,
                    )


med_scheduler = MedScheduler()
