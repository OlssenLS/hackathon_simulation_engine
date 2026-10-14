import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import User, ElderProfile

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

AMBIGUOUS_CHARS = set("0O1I")
LINK_CODE_CHARS = [c for c in string.ascii_uppercase + string.digits if c not in AMBIGUOUS_CHARS]


def generate_link_code(length: int = 6) -> str:
    return "".join(random.choices(LINK_CODE_CHARS, k=length))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token otentikasi tidak valid atau telah kedaluwarsa",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user


async def verify_elder_access(
    elder_id: str,
    user: User,
    db: AsyncSession,
    allow_elder_self: bool = True
) -> ElderProfile:
    """Verifies that the current user has permission to access the elder's data.

    - If user is caregiver: must be linked to this elder (caregiver_id == user.id).
    - If user is elder: must be their own id (elder_id == user.id) and allow_elder_self=True.
    Raises 403 or 404 otherwise.
    """
    stmt = select(ElderProfile).where(ElderProfile.user_id == elder_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data lansia tidak ditemukan"
        )

    if user.role == "caregiver":
        if profile.caregiver_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Anda tidak memiliki izin untuk mengakses lansia ini"
            )
    elif user.role == "elder":
        if not allow_elder_self or user.id != elder_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses ditolak"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Peran pengguna tidak dikenali"
        )

    return profile


def require_caregiver(user: User = Depends(get_current_user)) -> User:
    if user.role != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya pengasuh (caregiver) yang dapat melakukan tindakan ini"
        )
    return user
