import math
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User, ElderProfile, Vital, Measurement, Location, Alert
from app.schemas import (
    LatestVitalsResponse,
    VitalsPoint,
    BPData,
    GlucoseData,
    MeasurementCreate,
    MeasurementOut,
    LocationOut,
    LocationTrailPoint,
)
from app.security import get_current_user, verify_elder_access
from app.services.anomaly import anomaly_engine
from app.ws import manager

router = APIRouter(prefix="/elders", tags=["vitals"])


@router.get("/{elder_id}/vitals/latest", response_model=LatestVitalsResponse)
async def get_latest_vitals(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    # Latest vitals row
    stmt_v = select(Vital).where(Vital.elder_id == elder_id).order_by(Vital.ts.desc()).limit(1)
    v_row = (await db.execute(stmt_v)).scalar_one_or_none()

    # Latest BP
    stmt_bp = (
        select(Measurement)
        .where(and_(Measurement.elder_id == elder_id, Measurement.kind == "bp"))
        .order_by(Measurement.ts.desc())
        .limit(1)
    )
    bp_row = (await db.execute(stmt_bp)).scalar_one_or_none()

    # Latest Glucose
    stmt_g = (
        select(Measurement)
        .where(and_(Measurement.elder_id == elder_id, Measurement.kind == "glucose"))
        .order_by(Measurement.ts.desc())
        .limit(1)
    )
    g_row = (await db.execute(stmt_g)).scalar_one_or_none()

    # Worst severity among unacknowledged alerts in the last 30 min
    thirty_min_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    stmt_alerts = (
        select(Alert.severity)
        .where(
            and_(
                Alert.elder_id == elder_id,
                Alert.acknowledged == False,
                Alert.ts >= thirty_min_ago,
            )
        )
    )
    severities = (await db.execute(stmt_alerts)).scalars().all()
    overall_status = "normal"
    if "critical" in severities:
        overall_status = "critical"
    elif "warning" in severities:
        overall_status = "warning"

    v_point = None
    if v_row:
        v_point = VitalsPoint(
            ts=v_row.ts,
            hr=v_row.hr,
            spo2=v_row.spo2,
            steps=v_row.steps,
            temp=v_row.temp,
            resp_rate=v_row.resp_rate,
            accel_mag=v_row.accel_mag,
            activity=v_row.activity,
        )

    bp_data = None
    if bp_row:
        bp_data = BPData(
            ts=bp_row.ts,
            systolic=bp_row.value1,
            diastolic=bp_row.value2 or 0.0,
        )

    g_data = None
    if g_row:
        g_data = GlucoseData(
            ts=g_row.ts,
            value=g_row.value1,
            context=g_row.context,
        )

    return LatestVitalsResponse(
        vitals=v_point,
        bp=bp_data,
        glucose=g_data,
        status=overall_status,
    )


@router.get("/{elder_id}/measurements", response_model=List[MeasurementOut])
async def get_measurements_history(
    elder_id: str,
    kind: Optional[str] = Query(None, pattern="^(bp|glucose|weight)$"),
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = (
        select(Measurement)
        .where(and_(Measurement.elder_id == elder_id, Measurement.ts >= since))
    )
    if kind:
        stmt = stmt.where(Measurement.kind == kind)
    stmt = stmt.order_by(Measurement.ts.desc())

    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/{elder_id}/measurements", response_model=MeasurementOut, status_code=status.HTTP_201_CREATED)
async def create_manual_measurement(
    elder_id: str,
    req: MeasurementCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    profile = await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    stmt_user = select(User).where(User.id == elder_id)
    elder_user = (await db.execute(stmt_user)).scalar_one()

    now = datetime.now(timezone.utc)
    meas = Measurement(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        ts=now,
        kind=req.kind,
        value1=req.value1,
        value2=req.value2,
        context=req.context,
        source="manual",
    )
    db.add(meas)
    await db.commit()
    await db.refresh(meas)

    # Anomaly check
    await anomaly_engine.process_measurement(
        elder_id=elder_id,
        elder_name=elder_user.name,
        kind=req.kind,
        value1=req.value1,
        value2=req.value2,
        context=req.context,
        now=now,
    )

    # WS broadcast
    ts_str = now.isoformat().replace("+00:00", "Z")
    await manager.broadcast_to_elder(
        elder_id=elder_id,
        event_type="measurement",
        data={
            "id": meas.id,
            "kind": meas.kind,
            "value1": meas.value1,
            "value2": meas.value2,
            "context": meas.context,
            "source": "manual",
        },
        ts=ts_str,
    )

    return meas


@router.get("/{elder_id}/location/latest", response_model=LocationOut)
async def get_latest_location(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    profile = await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(Location).where(Location.elder_id == elder_id).order_by(Location.ts.desc()).limit(1)
    loc = (await db.execute(stmt)).scalar_one_or_none()

    if not loc:
        # Default to home
        return LocationOut(
            lat=profile.home_lat,
            lng=profile.home_lng,
            ts=datetime.now(timezone.utc),
            distance_from_home_m=0.0,
            inside_geofence=True,
        )

    # Calculate distance from home
    dlat = (loc.lat - profile.home_lat) * 111320
    dlng = (loc.lng - profile.home_lng) * 111320 * math.cos(profile.home_lat * math.pi / 180)
    dist_m = round(math.sqrt(dlat**2 + dlng**2), 1)

    return LocationOut(
        lat=loc.lat,
        lng=loc.lng,
        ts=loc.ts,
        distance_from_home_m=dist_m,
        inside_geofence=dist_m <= profile.geofence_m,
    )


@router.get("/{elder_id}/location/trail", response_model=List[LocationTrailPoint])
async def get_location_trail(
    elder_id: str,
    minutes: int = Query(60, ge=1, le=1440),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    stmt = (
        select(Location)
        .where(and_(Location.elder_id == elder_id, Location.ts >= since))
        .order_by(Location.ts.asc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    return [LocationTrailPoint(lat=r.lat, lng=r.lng, ts=r.ts) for r in rows]
