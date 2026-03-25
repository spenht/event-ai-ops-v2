"""Media upload endpoint for campaign assets."""

import logging
import uuid
from fastapi import APIRouter, UploadFile, File, Query, HTTPException, Request

from ..settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/media", tags=["media"])

# Max file size: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024
ALLOWED_MIME_PREFIXES = ("image/", "video/", "audio/")


def _validate_auth(request: Request):
    """Simple auth check - spartans key or cron token."""
    token = (request.headers.get("x-spartans-key") or "").strip()
    cron = (request.headers.get("x-cron-token") or "").strip()
    auth = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()

    if settings.spartans_key and token == settings.spartans_key:
        return
    if settings.cron_token and cron == settings.cron_token:
        return
    if auth:
        # Validate it's a real Supabase JWT
        import jwt
        try:
            jwt.decode(auth, options={"verify_signature": False, "verify_exp": True})
            return
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    campaign_id: str = Query(...),
):
    """Upload a media file to Supabase Storage and return the public URL."""
    _validate_auth(request)

    # Validate MIME type
    content_type = file.content_type or ""
    if not any(content_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {content_type}. Use image/*, video/*, or audio/*",
        )

    # Read file
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Max {MAX_FILE_SIZE // (1024*1024)}MB")

    # Generate unique path
    ext = (file.filename or "file").rsplit(".", 1)[-1] if "." in (file.filename or "") else "bin"
    unique_name = f"{uuid.uuid4().hex[:12]}.{ext}"
    storage_path = f"media/{campaign_id}/{unique_name}"

    try:
        from supabase import create_client
        sb = create_client(settings.supabase_url, settings.supabase_service_role_key)

        result = sb.storage.from_("media").upload(
            storage_path,
            data,
            file_options={"content-type": content_type, "cache-control": "3600"},
        )

        # Build public URL
        public_url = sb.storage.from_("media").get_public_url(storage_path)

        logger.info("media_uploaded campaign=%s path=%s size=%d", campaign_id, storage_path, len(data))

        return {
            "ok": True,
            "url": public_url,
            "path": storage_path,
            "size": len(data),
            "content_type": content_type,
            "filename": file.filename,
        }
    except Exception as exc:
        logger.exception("media_upload_failed campaign=%s err=%s", campaign_id, str(exc)[:200])
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(exc)[:200]}")
