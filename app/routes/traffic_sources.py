"""Traffic Sources + Forms CRUD endpoints.

Allows unlimited traffic sources (pixels, ad platforms) and forms per campaign.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb

logger = logging.getLogger("traffic_sources")

router = APIRouter(tags=["traffic_sources"])


# ── Pydantic models ─────────────────────────────────────────────

class TrafficSourceCreate(BaseModel):
    name: str
    platform: str = "meta"
    pixel_id: str = ""
    api_token: str = ""
    utm_source: str = ""
    utm_medium: str = ""


class TrafficSourceUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    pixel_id: Optional[str] = None
    api_token: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    is_active: Optional[bool] = None


class FormCreate(BaseModel):
    name: str = "Default"
    slug: str
    traffic_source_id: Optional[str] = None
    config: dict = {}


class FormUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    traffic_source_id: Optional[str] = None
    config: Optional[dict] = None
    is_active: Optional[bool] = None


# ── Helpers ─────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Generate a URL-safe slug."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── Traffic Sources CRUD ────────────────────────────────────────

@router.get("/v1/campaigns/{campaign_id}/traffic-sources")
async def list_traffic_sources(campaign_id: str):
    """List all traffic sources for a campaign."""
    res = (
        sb.table("traffic_sources")
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("created_at", desc=False)
        .execute()
    )
    return {"data": res.data or []}


@router.post("/v1/campaigns/{campaign_id}/traffic-sources", status_code=201)
async def create_traffic_source(campaign_id: str, body: TrafficSourceCreate):
    """Create a new traffic source for a campaign."""
    row = {
        "campaign_id": campaign_id,
        "name": body.name,
        "platform": body.platform,
        "pixel_id": body.pixel_id,
        "api_token": body.api_token,
        "utm_source": body.utm_source,
        "utm_medium": body.utm_medium,
    }
    res = sb.table("traffic_sources").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create traffic source")
    return {"data": res.data[0]}


@router.patch("/v1/traffic-sources/{source_id}")
async def update_traffic_source(source_id: str, body: TrafficSourceUpdate):
    """Update a traffic source."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = (
        sb.table("traffic_sources")
        .update(updates)
        .eq("id", source_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Traffic source not found")
    return {"data": res.data[0]}


@router.delete("/v1/traffic-sources/{source_id}")
async def delete_traffic_source(source_id: str):
    """Delete (deactivate) a traffic source."""
    res = (
        sb.table("traffic_sources")
        .update({"is_active": False})
        .eq("id", source_id)
        .execute()
    )
    return {"ok": True}


# ── Forms CRUD ──────────────────────────────────────────────────

@router.get("/v1/campaigns/{campaign_id}/forms")
async def list_forms(campaign_id: str):
    """List all forms for a campaign."""
    res = (
        sb.table("forms")
        .select("*, traffic_sources(name, platform)")
        .eq("campaign_id", campaign_id)
        .order("created_at", desc=False)
        .execute()
    )
    return {"data": res.data or []}


@router.post("/v1/campaigns/{campaign_id}/forms", status_code=201)
async def create_form(campaign_id: str, body: FormCreate):
    """Create a new form for a campaign."""
    slug = _slugify(body.slug) if body.slug else _slugify(body.name)
    row = {
        "campaign_id": campaign_id,
        "name": body.name,
        "slug": slug,
        "traffic_source_id": body.traffic_source_id,
        "config": body.config,
    }
    try:
        res = sb.table("forms").insert(row).execute()
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Form slug '{slug}' already exists for this campaign")
        raise
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create form")
    return {"data": res.data[0]}


@router.patch("/v1/forms/{form_id}")
async def update_form(form_id: str, body: FormUpdate):
    """Update a form."""
    updates = body.model_dump(exclude_none=True)
    if "slug" in updates:
        updates["slug"] = _slugify(updates["slug"])
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = (
        sb.table("forms")
        .update(updates)
        .eq("id", form_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Form not found")
    return {"data": res.data[0]}


@router.delete("/v1/forms/{form_id}")
async def delete_form(form_id: str):
    """Delete (deactivate) a form."""
    res = (
        sb.table("forms")
        .update({"is_active": False})
        .eq("id", form_id)
        .execute()
    )
    return {"ok": True}
