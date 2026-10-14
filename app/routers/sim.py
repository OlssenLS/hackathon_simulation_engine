from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import User, ElderProfile, Vital, Location, Measurement, MedLog, Alert, Medication
from app.schemas import SimTriggerRequest, SimStatusResponse
from app.security import get_current_user
from app.services.simulator import simulator
from app.seed import generate_backfill_for_elder

router = APIRouter(prefix="/sim", tags=["simulation"])


@router.post("/{elder_id}/trigger", response_model=SimStatusResponse)
async def trigger_sim_scenario(
    elder_id: str,
    req: SimTriggerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Verify elder exists (no strict role restriction per Section 8.9)
    stmt = select(ElderProfile).where(ElderProfile.user_id == elder_id)
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lansia tidak ditemukan"
        )

    res = await simulator.trigger_scenario(elder_id, req.scenario, req.duration_s)
    if not res:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gagal memicu skenario simulasi"
        )

    return SimStatusResponse(
        scenario=res["scenario"],
        activity=res["activity"],
        duration_remaining_s=res["duration_remaining_s"],
    )


@router.get("/{elder_id}/status", response_model=SimStatusResponse)
async def get_sim_status(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ElderProfile).where(ElderProfile.user_id == elder_id)
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lansia tidak ditemukan"
        )

    status_data = simulator.get_status(elder_id)
    if not status_data:
        return SimStatusResponse(
            scenario="normal",
            activity="resting",
            duration_remaining_s=0,
        )

    return SimStatusResponse(
        scenario=status_data["scenario"],
        activity=status_data["activity"],
        duration_remaining_s=status_data["duration_remaining_s"],
    )


@router.post("/{elder_id}/reseed")
async def reseed_elder_history(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(ElderProfile)
        .options(selectinload(ElderProfile.user))
        .where(ElderProfile.user_id == elder_id)
    )
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lansia tidak ditemukan"
        )

    elder_user = profile.user

    # Fetch active medications
    meds_stmt = select(Medication).where(Medication.elder_id == elder_id)
    meds = (await db.execute(meds_stmt)).scalars().all()

    # Clear existing history for elder
    await db.execute(delete(Vital).where(Vital.elder_id == elder_id))
    await db.execute(delete(Location).where(Location.elder_id == elder_id))
    await db.execute(delete(Measurement).where(Measurement.elder_id == elder_id))
    await db.execute(delete(MedLog).where(MedLog.elder_id == elder_id))
    await db.execute(delete(Alert).where(Alert.elder_id == elder_id))
    await db.commit()

    # Regenerate 7 days
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=7)
    await generate_backfill_for_elder(
        db=db,
        elder_id=elder_id,
        name=elder_user.name,
        conditions=profile.conditions or [],
        home_lat=profile.home_lat,
        home_lng=profile.home_lng,
        medications=meds,
        start_dt=start_time,
        end_dt=now,
    )

    # Re-init simulator state
    simulator.states[elder_id] = simulator.init_elder_state(profile, elder_user, now)

    return {"status": "ok", "message": f"7 hari riwayat untuk {elder_user.name} berhasil digenerate ulang"}
