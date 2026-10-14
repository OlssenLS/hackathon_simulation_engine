import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User, ElderProfile, DoctorAppt
from app.schemas import AppointmentCreate, AppointmentUpdate, AppointmentOut
from app.security import get_current_user, verify_elder_access

router = APIRouter(prefix="/elders", tags=["appointments"])


@router.get("/{elder_id}/appointments", response_model=List[AppointmentOut])
async def list_appointments(
    elder_id: str,
    upcoming: Optional[bool] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(DoctorAppt).where(DoctorAppt.elder_id == elder_id)
    if upcoming:
        now = datetime.now(timezone.utc)
        stmt = stmt.where(DoctorAppt.ts >= now)

    stmt = stmt.order_by(DoctorAppt.ts.asc())
    results = await db.execute(stmt)
    return results.scalars().all()


@router.post("/{elder_id}/appointments", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    elder_id: str,
    req: AppointmentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    appt = DoctorAppt(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        doctor=req.doctor,
        location=req.location,
        ts=req.ts,
        notes=req.notes,
    )
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return appt


@router.put("/{elder_id}/appointments/{appt_id}", response_model=AppointmentOut)
async def update_appointment(
    elder_id: str,
    appt_id: str,
    req: AppointmentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(DoctorAppt).where(and_(DoctorAppt.id == appt_id, DoctorAppt.elder_id == elder_id))
    appt = (await db.execute(stmt)).scalar_one_or_none()
    if not appt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal konsultasi dokter tidak ditemukan"
        )

    if req.doctor is not None:
        appt.doctor = req.doctor
    if req.location is not None:
        appt.location = req.location
    if req.ts is not None:
        appt.ts = req.ts
    if req.notes is not None:
        appt.notes = req.notes

    await db.commit()
    await db.refresh(appt)
    return appt


@router.delete("/{elder_id}/appointments/{appt_id}")
async def delete_appointment(
    elder_id: str,
    appt_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(DoctorAppt).where(and_(DoctorAppt.id == appt_id, DoctorAppt.elder_id == elder_id))
    appt = (await db.execute(stmt)).scalar_one_or_none()
    if not appt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal konsultasi dokter tidak ditemukan"
        )

    await db.delete(appt)
    await db.commit()
    return {"status": "ok", "message": "Jadwal konsultasi berhasil dihapus"}
