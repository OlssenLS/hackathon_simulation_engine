import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Alert, User, ElderProfile
from app.schemas import AlertOut, SOSRequest
from app.security import get_current_user, verify_elder_access
from app.ws import manager

router = APIRouter(tags=["alerts"])


@router.get("/elders/{elder_id}/alerts", response_model=List[AlertOut])
async def list_elder_alerts(
    elder_id: str,
    severity: Optional[str] = Query(None, pattern="^(info|warning|critical)$"),
    acknowledged: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(Alert).where(Alert.elder_id == elder_id)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged == acknowledged)

    stmt = stmt.order_by(Alert.ts.desc()).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Alert).where(Alert.id == alert_id)
    alert = (await db.execute(stmt)).scalar_one_or_none()
    if not alert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Peringatan tidak ditemukan"
        )

    # Verify access to this elder
    await verify_elder_access(alert.elder_id, user, db, allow_elder_self=True)

    now = datetime.now(timezone.utc)
    alert.acknowledged = True
    alert.acknowledged_at = now
    await db.commit()
    await db.refresh(alert)

    return alert


@router.post("/elders/{elder_id}/sos", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
async def trigger_manual_sos(
    elder_id: str,
    req: Optional[SOSRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(User).where(User.id == elder_id)
    elder_user = (await db.execute(stmt)).scalar_one()

    now = datetime.now(timezone.utc)
    note_text = f" Catatan: {req.note}" if req and req.note else ""
    msg = f"Tombol Darurat (SOS) manual diaktifkan oleh {elder_user.name}!{note_text}"

    alert = Alert(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        type="sos_manual",
        severity="critical",
        message=msg,
        payload={"note": req.note if req else None, "triggered_by": user.id},
        source="sos",
        ts=now,
        acknowledged=False,
        acknowledged_at=None,
    )
    db.add(alert)
    await db.commit()
    await db.refresh(alert)

    # Broadcast over WS
    ts_str = now.isoformat().replace("+00:00", "Z")
    await manager.broadcast_to_elder(
        elder_id=elder_id,
        event_type="alert",
        data={
            "id": alert.id,
            "elder_id": alert.elder_id,
            "type": alert.type,
            "severity": alert.severity,
            "message": alert.message,
            "payload": alert.payload,
            "source": alert.source,
            "ts": ts_str,
            "acknowledged": False,
            "acknowledged_at": None,
        },
        ts=ts_str,
    )

    return alert
