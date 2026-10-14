import uuid
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Document, User, ElderProfile
from app.schemas import DocumentOut
from app.security import get_current_user, verify_elder_access
from app.services import storage

router = APIRouter(prefix="/elders", tags=["documents"])

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
}


@router.post("/{elder_id}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    elder_id: str,
    title: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    # Validate mime
    content_type = file.content_type or ""
    if content_type.lower() not in ALLOWED_MIME_TYPES:
        # Check filename extension
        fn_lower = file.filename.lower()
        if not (fn_lower.endswith(".pdf") or fn_lower.endswith(".png") or fn_lower.endswith(".jpg") or fn_lower.endswith(".jpeg")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Format file tidak didukung. Hanya file PDF, PNG, atau JPG yang diperbolehkan."
            )
        # Infer mime
        if fn_lower.endswith(".pdf"):
            content_type = "application/pdf"
        elif fn_lower.endswith(".png"):
            content_type = "image/png"
        else:
            content_type = "image/jpeg"

    # Read content
    content = await file.read()
    size_bytes = len(content)

    if size_bytes > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ukuran file melebihi batas maksimal 5 MB ({size_bytes} bytes)."
        )

    # Upload file
    file_url = await storage.upload_file(file.filename, content, content_type)

    now = datetime.now(timezone.utc)
    doc = Document(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        title=title,
        file_url=file_url,
        mime=content_type,
        size_bytes=size_bytes,
        uploaded_at=now,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    return doc


@router.get("/{elder_id}/documents", response_model=List[DocumentOut])
async def list_documents(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(Document).where(Document.elder_id == elder_id).order_by(Document.uploaded_at.desc())
    results = await db.execute(stmt)
    return results.scalars().all()


@router.delete("/{elder_id}/documents/{doc_id}")
async def delete_document(
    elder_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)

    stmt = select(Document).where(and_(Document.id == doc_id, Document.elder_id == elder_id))
    doc = (await db.execute(stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dokumen tidak ditemukan"
        )

    # Delete storage file
    await storage.delete_file(doc.file_url)

    await db.delete(doc)
    await db.commit()

    return {"status": "ok", "message": "Dokumen berhasil dihapus"}
