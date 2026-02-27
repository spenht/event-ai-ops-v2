from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..services.tickets import lookup_ticket

router = APIRouter(prefix="/v1/tickets", tags=["tickets"])


@router.get("/{ticket_id}.png")
def get_ticket_png(ticket_id: str, t: str = Query(default="", description="access token")):
    rec = lookup_ticket(ticket_id)
    if not rec:
        raise HTTPException(status_code=404, detail="ticket not found")

    if not t or t != rec.get("token"):
        raise HTTPException(status_code=403, detail="forbidden")

    fp = Path(rec.get("file") or "")
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="file missing")

    # Twilio reads content-type to classify media.
    return FileResponse(
        str(fp),
        media_type="image/png",
        filename=fp.name,
        headers={"Cache-Control": "no-store"},
    )
