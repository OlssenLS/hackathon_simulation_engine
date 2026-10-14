import math
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_, or_, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import User, ElderProfile, Vital
from app.schemas import (
    LinkElderRequest,
    ElderProfileOut,
    CaregiverContact,
)
from app.security import (
    get_current_user,
    require_caregiver,
    verify_elder_access,
)

router = APIRouter(prefix="/elders", tags=["elders"])


def calculate_age(birth_date: Optional[date]) -> Optional[int]:
    if not birth_date:
        return None
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


@router.post("/link", response_model=ElderProfileOut)
async def link_elder(
    req: LinkElderRequest,
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(ElderProfile)
        .options(selectinload(ElderProfile.user))
        .where(ElderProfile.link_code == req.link_code.upper().strip())
    )
    profile = (await db.execute(stmt)).scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kode tautan tidak ditemukan"
        )

    profile.caregiver_id = caregiver.id
    await db.commit()
    await db.refresh(profile)

    elder_user = profile.user
    age = calculate_age(profile.birth_date)

    return ElderProfileOut(
        id=profile.user_id,
        name=elder_user.name,
        age=age,
        birth_date=profile.birth_date,
        conditions=profile.conditions or [],
        phone=elder_user.phone,
        doctor_name=profile.doctor_name,
        home_lat=profile.home_lat,
        home_lng=profile.home_lng,
        geofence_m=profile.geofence_m,
        link_code=profile.link_code,
        caregiver_id=profile.caregiver_id,
    )


@router.get("", response_model=List[ElderProfileOut])
async def list_linked_elders(
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(ElderProfile)
        .options(selectinload(ElderProfile.user))
        .where(ElderProfile.caregiver_id == caregiver.id)
    )
    profiles = (await db.execute(stmt)).scalars().all()

    out = []
    for p in profiles:
        age = calculate_age(p.birth_date)
        out.append(
            ElderProfileOut(
                id=p.user_id,
                name=p.user.name,
                age=age,
                birth_date=p.birth_date,
                conditions=p.conditions or [],
                phone=p.user.phone,
                doctor_name=p.doctor_name,
                home_lat=p.home_lat,
                home_lng=p.home_lng,
                geofence_m=p.geofence_m,
                link_code=p.link_code,
                caregiver_id=p.caregiver_id,
            )
        )
    return out


@router.get("/{elder_id}", response_model=ElderProfileOut)
async def get_elder_profile(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    profile = await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    stmt = select(User).where(User.id == elder_id)
    elder_user = (await db.execute(stmt)).scalar_one()

    age = calculate_age(profile.birth_date)
    return ElderProfileOut(
        id=profile.user_id,
        name=elder_user.name,
        age=age,
        birth_date=profile.birth_date,
        conditions=profile.conditions or [],
        phone=elder_user.phone,
        doctor_name=profile.doctor_name,
        home_lat=profile.home_lat,
        home_lng=profile.home_lng,
        geofence_m=profile.geofence_m,
        link_code=profile.link_code,
        caregiver_id=profile.caregiver_id,
    )


@router.get("/{elder_id}/caregiver", response_model=CaregiverContact)
async def get_elder_caregiver_contact(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    profile = await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    if not profile.caregiver_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lansia belum terhubung dengan keluarga"
        )

    stmt = select(User).where(User.id == profile.caregiver_id)
    caregiver = (await db.execute(stmt)).scalar_one_or_none()
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data keluarga tidak ditemukan"
        )

    return CaregiverContact(
        id=caregiver.id,
        name=caregiver.name,
        phone=caregiver.phone,
    )


@router.get("/{elder_id}/vitals")
async def get_vitals_history(
    elder_id: str,
    from_ts: Optional[datetime] = Query(None, alias="from"),
    to_ts: Optional[datetime] = Query(None, alias="to"),
    interval: str = Query("5m", pattern="^(raw|1m|5m|1h)$"),
    metrics: Optional[str] = Query(None),  # e.g. "hr,spo2"
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    now = datetime.now(timezone.utc)
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - timedelta(days=7)

    # Base query
    stmt = (
        select(Vital)
        .where(
            and_(
                Vital.elder_id == elder_id,
                Vital.ts >= from_ts,
                Vital.ts <= to_ts
            )
        )
        .order_by(Vital.ts.asc())
    )

    result = await db.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        return []

    # Downsample if needed
    interval_seconds = {
        "raw": 0,
        "1m": 60,
        "5m": 300,
        "1h": 3600
    }.get(interval, 300)

    requested_metrics = set([m.strip() for m in metrics.split(",") if m.strip()]) if metrics else None

    if interval_seconds == 0 or len(rows) <= 2000 and interval == "raw":
        # Raw rows
        output = []
        step = max(1, len(rows) // 2000)
        for row in rows[::step]:
            item = {"ts": row.ts.isoformat().replace("+00:00", "Z")}
            for field in ["hr", "spo2", "steps", "temp", "resp_rate", "accel_mag", "activity"]:
                if requested_metrics is None or field in requested_metrics:
                    item[field] = getattr(row, field)
            output.append(item)
        return output[:2000]

    # Downsampling by buckets
    buckets = {}
    for row in rows:
        ts_epoch = int(row.ts.timestamp())
        bucket_epoch = (ts_epoch // interval_seconds) * interval_seconds
        if bucket_epoch not in buckets:
            buckets[bucket_epoch] = []
        buckets[bucket_epoch].append(row)

    output = []
    sorted_bucket_keys = sorted(buckets.keys())
    # Cap at 2000 points
    step = max(1, len(sorted_bucket_keys) // 2000)
    for b_epoch in sorted_bucket_keys[::step]:
        b_rows = buckets[b_epoch]
        b_ts = datetime.fromtimestamp(b_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        n = len(b_rows)
        item = {
            "ts": b_ts,
            "hr": round(sum(r.hr for r in b_rows) / n, 1),
            "spo2": round(sum(r.spo2 for r in b_rows) / n, 1),
            "steps": max(r.steps for r in b_rows),
            "temp": round(sum(r.temp for r in b_rows) / n, 2),
            "resp_rate": round(sum(r.resp_rate for r in b_rows) / n, 1),
            "accel_mag": round(sum(r.accel_mag for r in b_rows) / n, 2),
            "activity": b_rows[-1].activity,
        }
        if requested_metrics:
            filtered_item = {"ts": b_ts}
            for m in requested_metrics:
                if m in item:
                    filtered_item[m] = item[m]
            output.append(filtered_item)
        else:
            output.append(item)

    return output[:2000]
