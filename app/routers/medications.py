import uuid
from datetime import datetime, timezone, timedelta, date, time
from zoneinfo import ZoneInfo
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import User, ElderProfile, Medication, MedLog
from app.schemas import (
    MedicationCreate,
    MedicationUpdate,
    MedicationOut,
    MedLogOut,
    AdherenceResponse,
)
from app.security import (
    get_current_user,
    verify_elder_access,
    require_caregiver,
)
from app.services.med_scheduler import med_scheduler
from app.ws import manager

router = APIRouter(tags=["medications"])
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")


@router.get("/elders/{elder_id}/medications", response_model=List[MedicationOut])
async def list_medications(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    stmt = (
        select(Medication)
        .where(and_(Medication.elder_id == elder_id, Medication.active == True))
        .order_by(Medication.name.asc())
    )
    results = await db.execute(stmt)
    return results.scalars().all()


@router.post("/elders/{elder_id}/medications", response_model=MedicationOut, status_code=status.HTTP_201_CREATED)
async def create_medication(
    elder_id: str,
    req: MedicationCreate,
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, caregiver, db, allow_elder_self=False)

    med = Medication(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        name=req.name,
        dose=req.dose,
        times=req.times,
        days=req.days or [],
        notes=req.notes,
        active=True,
    )
    db.add(med)
    await db.commit()
    await db.refresh(med)

    # Immediately ensure today's logs exist for this newly created medication
    await med_scheduler.ensure_today_logs_for_med(med, db)

    return med


@router.put("/elders/{elder_id}/medications/{med_id}", response_model=MedicationOut)
async def update_medication(
    elder_id: str,
    med_id: str,
    req: MedicationUpdate,
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, caregiver, db, allow_elder_self=False)

    stmt = select(Medication).where(and_(Medication.id == med_id, Medication.elder_id == elder_id))
    med = (await db.execute(stmt)).scalar_one_or_none()
    if not med:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal obat tidak ditemukan"
        )

    if req.name is not None:
        med.name = req.name
    if req.dose is not None:
        med.dose = req.dose
    if req.times is not None:
        med.times = req.times
    if req.days is not None:
        med.days = req.days
    if req.notes is not None:
        med.notes = req.notes
    if req.active is not None:
        med.active = req.active

    await db.commit()
    await db.refresh(med)

    if med.active and req.times is not None:
        await med_scheduler.ensure_today_logs_for_med(med, db)

    return med


@router.delete("/elders/{elder_id}/medications/{med_id}")
async def delete_medication(
    elder_id: str,
    med_id: str,
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, caregiver, db, allow_elder_self=False)

    stmt = select(Medication).where(and_(Medication.id == med_id, Medication.elder_id == elder_id))
    med = (await db.execute(stmt)).scalar_one_or_none()
    if not med:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal obat tidak ditemukan"
        )

    # Soft delete
    med.active = False
    await db.commit()
    return {"status": "ok", "message": "Jadwal obat berhasil dinonaktifkan"}


@router.get("/elders/{elder_id}/med-logs", response_model=List[MedLogOut])
async def get_med_logs(
    elder_id: str,
    date_str: Optional[str] = Query(None, alias="date"),  # YYYY-MM-DD
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    now = datetime.now(timezone.utc)
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Format tanggal salah (gunakan YYYY-MM-DD)")
    else:
        target_date = now.astimezone(JAKARTA_TZ).date()

    # Calculate UTC bounds for this local day
    start_local = datetime.combine(target_date, time(0, 0, 0), tzinfo=JAKARTA_TZ)
    end_local = datetime.combine(target_date, time(23, 59, 59), tzinfo=JAKARTA_TZ)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    stmt = (
        select(MedLog, Medication)
        .join(Medication, MedLog.medication_id == Medication.id)
        .where(
            and_(
                MedLog.elder_id == elder_id,
                MedLog.scheduled_at >= start_utc,
                MedLog.scheduled_at <= end_utc,
            )
        )
        .order_by(MedLog.scheduled_at.asc())
    )
    results = (await db.execute(stmt)).all()

    out = []
    for log, med in results:
        out.append(
            MedLogOut(
                id=log.id,
                medication_id=log.medication_id,
                elder_id=log.elder_id,
                name=med.name,
                dose=med.dose,
                scheduled_at=log.scheduled_at,
                status=log.status,
                confirmed_at=log.confirmed_at,
            )
        )
    return out


@router.post("/med-logs/{log_id}/confirm", response_model=MedLogOut)
async def confirm_med_log(
    log_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(MedLog, Medication)
        .join(Medication, MedLog.medication_id == Medication.id)
        .where(MedLog.id == log_id)
    )
    result = (await db.execute(stmt)).first()
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Log minum obat tidak ditemukan"
        )
    log, med = result

    # Verify access to this elder
    await verify_elder_access(log.elder_id, user, db, allow_elder_self=True)

    now = datetime.now(timezone.utc)
    log.status = "taken"
    log.confirmed_at = now
    await db.commit()
    await db.refresh(log)

    # WS broadcast
    ts_str = now.isoformat().replace("+00:00", "Z")
    sched_str = log.scheduled_at.isoformat().replace("+00:00", "Z")
    await manager.broadcast_to_elder(
        elder_id=log.elder_id,
        event_type="med",
        data={
            "log_id": log.id,
            "name": med.name,
            "dose": med.dose,
            "status": "taken",
            "scheduled_at": sched_str,
            "confirmed_at": ts_str,
        },
        ts=ts_str,
    )

    return MedLogOut(
        id=log.id,
        medication_id=log.medication_id,
        elder_id=log.elder_id,
        name=med.name,
        dose=med.dose,
        scheduled_at=log.scheduled_at,
        status=log.status,
        confirmed_at=log.confirmed_at,
    )


@router.get("/elders/{elder_id}/medications/adherence", response_model=AdherenceResponse)
async def get_medication_adherence(
    elder_id: str,
    days: int = Query(7, ge=1, le=90),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(MedLog.status).where(
        and_(
            MedLog.elder_id == elder_id,
            MedLog.scheduled_at >= since
        )
    )
    statuses = (await db.execute(stmt)).scalars().all()

    taken = sum(1 for s in statuses if s == "taken")
    missed = sum(1 for s in statuses if s == "missed")
    pending = sum(1 for s in statuses if s == "pending")

    total_decided = taken + missed
    rate = round(taken / total_decided, 2) if total_decided > 0 else 1.0

    return AdherenceResponse(
        taken=taken,
        missed=missed,
        pending=pending,
        rate=rate,
    )
