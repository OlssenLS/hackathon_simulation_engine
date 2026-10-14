from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Report, User, ElderProfile
from app.schemas import ReportOut, ReportMetadata
from app.security import get_current_user, require_caregiver, verify_elder_access
from app.services.report import generate_smart_report

router = APIRouter(tags=["reports"])


@router.post("/elders/{elder_id}/reports/generate", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def generate_report(
    elder_id: str,
    days: int = Query(7, ge=1, le=30),
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, caregiver, db, allow_elder_self=False)
    report = await generate_smart_report(db, elder_id, days)
    return report


@router.get("/elders/{elder_id}/reports", response_model=List[ReportMetadata])
async def list_reports(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    stmt = (
        select(Report)
        .where(Report.elder_id == elder_id)
        .order_by(Report.created_at.desc())
    )
    results = await db.execute(stmt)
    return results.scalars().all()


@router.get("/reports/{report_id}", response_model=ReportOut)
async def get_report_detail(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Report).where(Report.id == report_id)
    report = (await db.execute(stmt)).scalar_one_or_none()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Laporan tidak ditemukan"
        )

    await verify_elder_access(report.elder_id, user, db, allow_elder_self=True)
    return report
