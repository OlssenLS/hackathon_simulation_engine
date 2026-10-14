from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import User, ElderProfile, Alert
from app.schemas import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    UserOut,
    MeResponse,
    ElderMeInfo,
    CaregiverContact,
    ElderSummaryForCaregiver,
)
from app.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    get_current_user,
    generate_link_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == req.email)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email sudah terdaftar"
        )

    user = User(
        name=req.name,
        email=req.email,
        password_hash=get_password_hash(req.password),
        role=req.role,
        phone=req.phone,
    )
    db.add(user)
    await db.flush()

    if req.role == "elder":
        # Generate unique link code
        code = generate_link_code()
        while (await db.execute(select(ElderProfile).where(ElderProfile.link_code == code))).scalar_one_or_none():
            code = generate_link_code()

        profile = ElderProfile(
            user_id=user.id,
            birth_date=req.birth_date,
            conditions=[],
            link_code=code,
            caregiver_id=None,
            doctor_name=None,
            home_lat=-6.2349,
            home_lng=106.9896,
            geofence_m=150.0,
            sim_enabled=True,
        )
        db.add(profile)

    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": user.id, "role": user.role})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == req.email)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email atau kata sandi salah"
        )

    token = create_access_token({"sub": user.id, "role": user.role})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


me_router = APIRouter(tags=["auth"])


@me_router.get("/me", response_model=MeResponse)
async def get_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    elder_info: Optional[ElderMeInfo] = None
    elders_list: Optional[List[ElderSummaryForCaregiver]] = None

    if user.role == "elder":
        stmt = (
            select(ElderProfile)
            .options(selectinload(ElderProfile.caregiver))
            .where(ElderProfile.user_id == user.id)
        )
        profile = (await db.execute(stmt)).scalar_one_or_none()
        if profile:
            cg_contact = None
            if profile.caregiver:
                cg_contact = CaregiverContact(
                    id=profile.caregiver.id,
                    name=profile.caregiver.name,
                    phone=profile.caregiver.phone,
                )
            elder_info = ElderMeInfo(
                id=profile.user_id,
                link_code=profile.link_code,
                caregiver=cg_contact,
            )

    elif user.role == "caregiver":
        stmt = (
            select(ElderProfile, User)
            .join(User, ElderProfile.user_id == User.id)
            .where(ElderProfile.caregiver_id == user.id)
        )
        results = (await db.execute(stmt)).all()
        elders_list = []
        now = datetime.now(timezone.utc)
        thirty_min_ago = now - timedelta(minutes=30)

        for profile, elder_user in results:
            # Check worst alert severity in last 30 min
            alert_stmt = (
                select(Alert.severity)
                .where(
                    and_(
                        Alert.elder_id == profile.user_id,
                        Alert.acknowledged == False,
                        Alert.ts >= thirty_min_ago
                    )
                )
                .order_by(Alert.ts.desc())
            )
            severities = (await db.execute(alert_stmt)).scalars().all()
            last_sev = None
            if "critical" in severities:
                last_sev = "critical"
            elif "warning" in severities:
                last_sev = "warning"
            elif "info" in severities:
                last_sev = "info"

            elders_list.append(
                ElderSummaryForCaregiver(
                    id=profile.user_id,
                    name=elder_user.name,
                    link_code=profile.link_code,
                    last_alert_severity=last_sev,
                    online=profile.sim_enabled,
                )
            )

    return MeResponse(
        user=UserOut.model_validate(user),
        elder=elder_info,
        elders=elders_list,
    )
