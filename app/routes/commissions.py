from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.commission_engine import (
    attribute_sale,
    get_agent_earnings,
    get_leaderboard,
    sync_all_attributions,
)

logger = logging.getLogger("commissions")

router = APIRouter(prefix="/v1/commissions", tags=["commissions"])


# ─── Auth helper ─────────────────────────────────────────────────────────────


def _validate_auth(request: Request, campaign_id: str | None = None) -> None:
    """
    Validate request authentication.

    Checks (in order):
    1. X-Cron-Token header against global cron_token
    2. X-Spartans-Key header against campaign-specific spartans_key (DB)
    3. X-Spartans-Key header against global settings.spartans_key (fallback)
    4. No token configured = open (dev mode)
    """
    token = (request.headers.get("x-cron-token") or "").strip()
    spartans_key = (request.headers.get("x-spartans-key") or "").strip()

    # 1. Global cron token
    if settings.cron_token and token == settings.cron_token:
        return

    # 2. Campaign-specific spartans key (from DB)
    if campaign_id and spartans_key:
        try:
            r = (
                sb.table("campaigns")
                .select("spartans_key")
                .eq("id", campaign_id)
                .limit(1)
                .execute()
            )
            campaign = (r.data or [None])[0]
            if campaign and campaign.get("spartans_key") and campaign["spartans_key"] == spartans_key:
                return
        except Exception:
            pass

    # 3. Global spartans_key fallback (covers campaigns without DB key)
    if spartans_key and settings.spartans_key and spartans_key == settings.spartans_key:
        return

    # 4. No token configured = open (dev mode)
    if not settings.cron_token:
        return

    raise HTTPException(status_code=403, detail="invalid auth token")


# ─── Request Models ──────────────────────────────────────────────────────────


class CommissionConfigBody(BaseModel):
    campaign_id: str
    tier: str = "VIP"
    commission_type: str = "fixed"  # "fixed" or "percentage"
    commission_value: float = 0


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/config")
async def list_commission_configs(request: Request, campaign_id: str):
    """List commission configs for a campaign."""
    _validate_auth(request, campaign_id)
    try:
        r = (
            sb.table("commission_configs")
            .select("*")
            .eq("campaign_id", campaign_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"ok": True, "data": r.data or []}
    except Exception as exc:
        logger.error("list_configs_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.post("/config")
async def upsert_commission_config(request: Request, body: CommissionConfigBody):
    """Upsert a commission config for a campaign + tier."""
    _validate_auth(request, body.campaign_id)
    try:
        # Check existing
        existing = (
            sb.table("commission_configs")
            .select("id")
            .eq("campaign_id", body.campaign_id)
            .eq("tier", body.tier)
            .limit(1)
            .execute()
        )
        record = {
            "campaign_id": body.campaign_id,
            "tier": body.tier,
            "commission_type": body.commission_type,
            "commission_value": body.commission_value,
        }
        if existing.data:
            r = (
                sb.table("commission_configs")
                .update(record)
                .eq("id", existing.data[0]["id"])
                .execute()
            )
        else:
            r = sb.table("commission_configs").insert(record).execute()
        return {"ok": True, "data": (r.data or [None])[0]}
    except Exception as exc:
        logger.error("upsert_config_failed campaign=%s err=%s", body.campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.get("/my-earnings")
async def my_earnings(request: Request, campaign_id: str, user_id: str):
    """Get earnings breakdown for the calling agent."""
    _validate_auth(request, campaign_id)
    try:
        earnings = await get_agent_earnings(user_id, campaign_id)
        return {"ok": True, "data": earnings}
    except Exception as exc:
        logger.error("my_earnings_failed user=%s err=%s", user_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.get("/leaderboard")
async def leaderboard(request: Request, campaign_id: str):
    """Get team leaderboard ranked by total commissions."""
    _validate_auth(request, campaign_id)
    try:
        data = await get_leaderboard(campaign_id)
        return {"ok": True, "data": data}
    except Exception as exc:
        logger.error("leaderboard_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.post("/sync")
async def sync_attributions(request: Request, campaign_id: str):
    """Backfill commissions for all PAID leads."""
    _validate_auth(request, campaign_id)
    try:
        result = await sync_all_attributions(campaign_id)
        return {"ok": True, "data": result}
    except Exception as exc:
        logger.error("sync_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.get("/export")
async def export_commissions(request: Request, campaign_id: str, status: Optional[str] = None):
    """Export commissions as CSV."""
    _validate_auth(request, campaign_id)
    try:
        q = sb.table("commissions").select("*").eq("campaign_id", campaign_id)
        if status:
            q = q.eq("status", status)
        r = q.order("created_at", desc=True).execute()
        rows = r.data or []

        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        else:
            output.write("No commissions found")

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=commissions_{campaign_id}.csv"},
        )
    except Exception as exc:
        logger.error("export_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.patch("/{commission_id}/approve")
async def approve_commission(request: Request, commission_id: str, campaign_id: str):
    """Approve a pending commission."""
    _validate_auth(request, campaign_id)
    try:
        r = (
            sb.table("commissions")
            .update({
                "status": "approved",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", commission_id)
            .eq("campaign_id", campaign_id)
            .execute()
        )
        updated = (r.data or [None])[0]
        if not updated:
            raise HTTPException(status_code=404, detail="commission not found")
        return {"ok": True, "data": updated}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("approve_failed id=%s err=%s", commission_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.patch("/{commission_id}/mark-paid")
async def mark_paid(request: Request, commission_id: str, campaign_id: str):
    """Mark an approved commission as paid."""
    _validate_auth(request, campaign_id)
    try:
        r = (
            sb.table("commissions")
            .update({
                "status": "paid",
                "paid_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", commission_id)
            .eq("campaign_id", campaign_id)
            .execute()
        )
        updated = (r.data or [None])[0]
        if not updated:
            raise HTTPException(status_code=404, detail="commission not found")
        return {"ok": True, "data": updated}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("mark_paid_failed id=%s err=%s", commission_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


# ── Commission Tiers (volume escalation) ──────────────────────────


def _campaign_id_from_config(config_id: str) -> str | None:
    """Resolve campaign_id from a commission_config row."""
    try:
        r = sb.table("commission_configs").select("campaign_id").eq("id", config_id).limit(1).execute()
        return (r.data or [{}])[0].get("campaign_id")
    except Exception:
        return None


@router.get("/config/{config_id}/tiers")
async def list_commission_tiers(config_id: str, request: Request, campaign_id: str = ""):
    """List escalation tiers for a commission config."""
    cid = campaign_id or _campaign_id_from_config(config_id)
    _validate_auth(request, cid)
    r = sb.table("commission_tiers").select("*").eq("config_id", config_id).order("min_sales").execute()
    return {"ok": True, "data": r.data or []}


@router.post("/config/{config_id}/tiers")
async def upsert_commission_tier(config_id: str, request: Request, campaign_id: str = ""):
    """Create or update an escalation tier."""
    cid = campaign_id or _campaign_id_from_config(config_id)
    _validate_auth(request, cid)
    body = await request.json()
    row = {
        "config_id": config_id,
        "min_sales": body.get("min_sales", 0),
        "commission_type": body.get("commission_type", "percentage"),
        "commission_value": body.get("commission_value", 0),
    }
    tier_id = body.get("id")
    if tier_id:
        sb.table("commission_tiers").update(row).eq("id", tier_id).execute()
    else:
        sb.table("commission_tiers").insert(row).execute()
    return {"ok": True}


@router.delete("/tiers/{tier_id}")
async def delete_commission_tier(tier_id: str, request: Request, campaign_id: str = ""):
    """Delete an escalation tier."""
    _validate_auth(request, campaign_id if campaign_id else None)
    sb.table("commission_tiers").delete().eq("id", tier_id).execute()
    return {"ok": True}


# ── Agents list + Bulk approve/pay ────────────────────────────────


@router.get("/agents")
async def list_agents_with_commissions(request: Request, campaign_id: str = Query(...)):
    """List all agents with their commission summary for a campaign."""
    _validate_auth(request, campaign_id)
    # Get all commissions grouped by agent
    r = sb.table("commissions").select("agent_id, tier, sale_amount, commission_amount, status").eq("campaign_id", campaign_id).execute()

    # Aggregate per agent
    agents: dict[str, dict] = {}
    for c in (r.data or []):
        aid = c["agent_id"]
        if aid not in agents:
            agents[aid] = {"agent_id": aid, "total_sales": 0, "total_earned": 0, "pending": 0, "approved": 0, "paid": 0, "sale_count": 0}
        agents[aid]["sale_count"] += 1
        agents[aid]["total_earned"] += float(c.get("commission_amount", 0))
        agents[aid][c.get("status", "pending")] += float(c.get("commission_amount", 0))

    # Get agent names from org_members
    agent_ids = list(agents.keys())
    if agent_ids:
        try:
            mr = sb.table("org_members").select("user_id, display_name, email").in_("user_id", agent_ids).execute()
            for m in (mr.data or []):
                uid = m["user_id"]
                if uid in agents:
                    agents[uid]["display_name"] = m.get("display_name") or m.get("email", "Agent")
        except Exception:
            pass

    # Get online status from spartan_sessions
    try:
        sr = sb.table("spartan_sessions").select("user_id, status").eq("campaign_id", campaign_id).in_("user_id", agent_ids).neq("status", "offline").execute()
        online_ids = {s["user_id"] for s in (sr.data or [])}
        for aid in agents:
            agents[aid]["online"] = aid in online_ids
    except Exception:
        pass

    return {"ok": True, "data": sorted(agents.values(), key=lambda x: x["total_earned"], reverse=True)}


@router.post("/bulk-approve")
async def bulk_approve_commissions(request: Request):
    """Approve multiple pending commissions at once."""
    _validate_auth(request)
    body = await request.json()
    commission_ids = body.get("commission_ids", [])
    approved_by = body.get("approved_by")

    if not commission_ids:
        # Approve ALL pending for a campaign
        campaign_id = body.get("campaign_id")
        agent_id = body.get("agent_id")  # optional filter
        if not campaign_id:
            raise HTTPException(status_code=400, detail="campaign_id or commission_ids required")
        q = sb.table("commissions").update({"status": "approved", "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": approved_by}).eq("campaign_id", campaign_id).eq("status", "pending")
        if agent_id:
            q = q.eq("agent_id", agent_id)
        r = q.execute()
        return {"ok": True, "updated": len(r.data or [])}

    count = 0
    for cid in commission_ids:
        try:
            sb.table("commissions").update({"status": "approved", "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": approved_by}).eq("id", cid).eq("status", "pending").execute()
            count += 1
        except Exception:
            pass
    return {"ok": True, "updated": count}


@router.post("/bulk-pay")
async def bulk_pay_commissions(request: Request):
    """Mark multiple approved commissions as paid."""
    _validate_auth(request)
    body = await request.json()
    commission_ids = body.get("commission_ids", [])
    payout_ref = body.get("payout_ref", "")

    if not commission_ids:
        campaign_id = body.get("campaign_id")
        agent_id = body.get("agent_id")
        if not campaign_id:
            raise HTTPException(status_code=400, detail="campaign_id or commission_ids required")
        q = sb.table("commissions").update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat(), "payout_ref": payout_ref}).eq("campaign_id", campaign_id).eq("status", "approved")
        if agent_id:
            q = q.eq("agent_id", agent_id)
        r = q.execute()
        return {"ok": True, "updated": len(r.data or [])}

    count = 0
    for cid in commission_ids:
        try:
            sb.table("commissions").update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat(), "payout_ref": payout_ref}).eq("id", cid).eq("status", "approved").execute()
            count += 1
        except Exception:
            pass
    return {"ok": True, "updated": count}
