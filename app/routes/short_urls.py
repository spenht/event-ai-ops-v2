"""Short URL redirect endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from ..deps import sb

logger = logging.getLogger("short_urls")

router = APIRouter(tags=["short_urls"])


@router.get("/v1/s/{code}")
async def redirect_short_url(code: str):
    """Resolve a short code and redirect (302) to the long URL."""
    if not code or len(code) > 50:
        raise HTTPException(status_code=404, detail="not found")

    try:
        r = (
            sb.table("short_urls")
            .select("long_url")
            .eq("code", code)
            .limit(1)
            .execute()
        )
        row = (r.data or [None])[0]
    except Exception:
        raise HTTPException(status_code=500, detail="lookup failed")

    if not row or not row.get("long_url"):
        raise HTTPException(status_code=404, detail="not found")

    return RedirectResponse(url=row["long_url"], status_code=302)
