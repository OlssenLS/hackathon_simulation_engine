import os
import uuid
import httpx
from typing import Optional
from app.config import settings

UPLOADS_DIR = "uploads"


async def upload_file(filename: str, content: bytes, mime_type: str) -> str:
    """Uploads file to Supabase Storage if configured, otherwise local ./uploads directory."""
    clean_name = f"{uuid.uuid4()}_{filename}"

    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY:
        try:
            url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{settings.SUPABASE_BUCKET}/{clean_name}"
            headers = {
                "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
                "Content-Type": mime_type,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, content=content, headers=headers)
                if res.status_code in (200, 201):
                    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{settings.SUPABASE_BUCKET}/{clean_name}"
        except Exception as e:
            print("Supabase upload failed, falling back to local:", e)

    # Local fallback
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    file_path = os.path.join(UPLOADS_DIR, clean_name)
    with open(file_path, "wb") as f:
        f.write(content)

    return f"/uploads/{clean_name}"


async def delete_file(file_url: str):
    """Deletes file from Supabase Storage or local ./uploads."""
    if file_url.startswith("http") and settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY:
        try:
            file_name = file_url.split("/")[-1]
            url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{settings.SUPABASE_BUCKET}/{file_name}"
            headers = {"Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(url, headers=headers)
        except Exception as e:
            print("Failed to delete from Supabase:", e)
    elif file_url.startswith("/uploads/"):
        file_name = file_url.replace("/uploads/", "")
        file_path = os.path.join(UPLOADS_DIR, file_name)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
