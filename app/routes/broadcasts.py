"""Broadcast campaign management endpoints.

Provides CRUD for WhatsApp template provisioning and broadcast campaigns.
All endpoints are protected by the cron token (X-Cron-Token header).

Supabase tables required (create manually):
---------------------------------------------------------------------------

-- Template registry
CREATE TABLE broadcast_templates (
    id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    content_sid   TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT 'es',
    category      TEXT NOT NULL DEFAULT 'MARKETING',
    status        TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    content_body  TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- Broadcast campaigns
CREATE TABLE broadcasts (
    id             UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    campaign_name  TEXT NOT NULL DEFAULT '',
    template_name  TEXT NOT NULL,
    audience_filter JSONB NOT NULL DEFAULT '{}',
    scheduled_at   TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | sending | completed | cancelled
    total_sent     INT NOT NULL DEFAULT 0,
    total_failed   INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT now()
);

---------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.whatsapp_templates import (
    TEMPLATES,
    create_all_templates,
    submit_for_approval,
    get_template_status,
    send_whatsapp_template,
)

logger = logging.getLogger("broadcasts")

router = APIRouter(prefix="/v1/broadcasts", tags=["broadcasts"])

# ---------------------------------------------------------------------------
# Auth helper (same pattern as automation.py)
# ---------------------------------------------------------------------------

def _validate_cron_token(request: Request) -> None:
    """Validate X-Cron-Token header if CRON_TOKEN is configured."""
    if not settings.cron_token:
        return  # No token configured = open (dev mode)
    token = (request.headers.get("x-cron-token") or "").strip()
    if token != settings.cron_token:
        raise HTTPException(status_code=403, detail="invalid cron token")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CampaignCreateRequest(BaseModel):
    template_name: str
    audience: str = "all"              # all | general | vip | unpaid_vip | not_confirmed
    scheduled_at: str                  # ISO-8601 with timezone
    campaign_name: str = ""            # optional friendly name


# ---------------------------------------------------------------------------
# Audience resolver
# ---------------------------------------------------------------------------

VALID_AUDIENCES = {"all", "general", "vip", "unpaid_vip", "not_confirmed"}


def _resolve_audience(audience: str) -> list[dict[str, Any]]:
    """Query leads matching the audience filter.

    Returns list of lead dicts with at least: lead_id, name, whatsapp, status, payment_status.
    """
    if audience not in VALID_AUDIENCES:
        raise HTTPException(status_code=400, detail=f"Invalid audience: {audience}. Valid: {VALID_AUDIENCES}")

    try:
        q = sb.table("leads").select("lead_id,name,whatsapp,status,payment_status")

        if audience == "all":
            # All leads with a whatsapp number
            q = q.neq("whatsapp", "")
        elif audience == "general":
            q = q.eq("status", "GENERAL_CONFIRMED").neq("payment_status", "PAID")
        elif audience == "vip":
            q = q.eq("payment_status", "PAID")
        elif audience == "unpaid_vip":
            q = q.in_("status", ["VIP_INTERESTED", "VIP_LINK_SENT"]).neq("payment_status", "PAID")
        elif audience == "not_confirmed":
            q = q.in_("status", ["NEW", ""])

        r = q.execute()
        leads = r.data or []
        # Extra filter: must have a non-empty whatsapp number
        return [l for l in leads if (l.get("whatsapp") or "").strip()]
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("audience_resolve_failed audience=%s", audience)
        raise HTTPException(status_code=500, detail=f"audience query failed: {str(e)[:200]}")


# ---------------------------------------------------------------------------
# Campaign sender (shared by manual trigger and automation)
# ---------------------------------------------------------------------------

async def execute_campaign(campaign_id: str) -> dict[str, Any]:
    """Execute a broadcast campaign: resolve audience, send templates, record touchpoints.

    This is called by both the manual trigger endpoint and the automation cron.

    Returns stats dict: {total, sent, failed}.
    """
    # Fetch campaign
    try:
        r = sb.table("broadcasts").select("*").eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
    except Exception as e:
        logger.exception("campaign_fetch_failed id=%s", campaign_id)
        raise HTTPException(status_code=500, detail=f"campaign fetch failed: {str(e)[:200]}")

    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")

    if campaign["status"] not in ("scheduled", "sending"):
        raise HTTPException(
            status_code=400,
            detail=f"campaign status is '{campaign['status']}', must be 'scheduled' or 'sending'",
        )

    template_name = campaign["template_name"]
    audience_filter = campaign.get("audience_filter") or {}
    audience = audience_filter.get("audience", "all")

    # Lookup content_sid from broadcast_templates table
    try:
        tr = (
            sb.table("broadcast_templates")
            .select("content_sid,status")
            .eq("name", template_name)
            .limit(1)
            .execute()
        )
        tmpl_row = (tr.data or [None])[0]
    except Exception as e:
        logger.exception("template_lookup_failed name=%s", template_name)
        raise HTTPException(status_code=500, detail=f"template lookup failed: {str(e)[:200]}")

    if not tmpl_row or not tmpl_row.get("content_sid"):
        raise HTTPException(status_code=400, detail=f"template '{template_name}' not provisioned")

    content_sid = tmpl_row["content_sid"]

    # Mark campaign as sending
    try:
        sb.table("broadcasts").update({"status": "sending"}).eq("id", campaign_id).execute()
    except Exception:
        logger.warning("campaign_status_update_failed id=%s", campaign_id)

    # Resolve audience
    leads = _resolve_audience(audience)
    total = len(leads)
    sent = 0
    failed = 0
    now = _utcnow()

    for lead in leads:
        lead_id = lead.get("lead_id", "")
        name = (lead.get("name") or "").strip() or "amigo/a"
        wa = (lead.get("whatsapp") or "").strip()

        if not wa:
            continue

        variables = {"1": name}

        try:
            msg_sid = await send_whatsapp_template(
                to_e164=wa,
                content_sid=content_sid,
                variables=variables,
            )
            sent += 1
            logger.info(
                "broadcast_sent campaign=%s template=%s lead=%s sid=%s",
                campaign_id, template_name, lead_id, msg_sid,
            )
        except Exception as e:
            failed += 1
            msg_sid = ""
            logger.error(
                "broadcast_send_failed campaign=%s lead=%s err=%s",
                campaign_id, lead_id, str(e)[:300],
            )

        # Record in touchpoints
        try:
            sb.table("touchpoints").insert({
                "lead_id": lead_id,
                "channel": "whatsapp",
                "event_type": "broadcast_sent",
                "payload": {
                    "campaign_id": campaign_id,
                    "template_name": template_name,
                    "content_sid": content_sid,
                    "audience": audience,
                    "sid": msg_sid,
                    "success": bool(msg_sid),
                },
            }).execute()
        except Exception:
            logger.warning("touchpoint_insert_failed campaign=%s lead=%s", campaign_id, lead_id)

    # Mark campaign as completed
    try:
        sb.table("broadcasts").update({
            "status": "completed",
            "total_sent": sent,
            "total_failed": failed,
        }).eq("id", campaign_id).execute()
    except Exception:
        logger.warning("campaign_complete_update_failed id=%s", campaign_id)

    logger.info(
        "campaign_completed id=%s template=%s total=%d sent=%d failed=%d",
        campaign_id, template_name, total, sent, failed,
    )

    return {"campaign_id": campaign_id, "total": total, "sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/templates/provision")
async def provision_templates(request: Request):
    """Create all templates via Twilio Content API and submit for approval.

    This is idempotent-ish: Twilio will reject duplicates by friendly_name,
    so re-running is safe (errors for existing templates are logged but not fatal).
    """
    _validate_cron_token(request)

    # 1. Create templates
    created = await create_all_templates()

    results = []

    for item in created:
        name = item["name"]
        content_sid = item.get("content_sid", "")
        body = item.get("body", "")
        language = item.get("language", "es")
        category = item.get("category", "MARKETING")
        error = item.get("error", "")

        # If creation failed, record the error
        if not content_sid:
            results.append({"name": name, "content_sid": "", "status": "create_failed", "error": error})
            continue

        # 2. Submit for approval
        approval_status = "pending"
        try:
            await submit_for_approval(content_sid, name, category)
        except Exception as e:
            logger.error("approval_submit_error name=%s err=%s", name, str(e)[:300])
            approval_status = "approval_submit_failed"

        # 3. Store in broadcast_templates table
        try:
            sb.table("broadcast_templates").upsert(
                {
                    "name": name,
                    "content_sid": content_sid,
                    "language": language,
                    "category": category,
                    "status": approval_status,
                    "content_body": body,
                },
                on_conflict="name",
            ).execute()
        except Exception as e:
            logger.error("template_db_save_error name=%s err=%s", name, str(e)[:300])

        results.append({"name": name, "content_sid": content_sid, "status": approval_status})

    return {"templates": results, "total": len(results)}


@router.get("/templates")
async def list_templates(request: Request):
    """List all provisioned templates with their latest approval status."""
    _validate_cron_token(request)

    try:
        r = sb.table("broadcast_templates").select("*").order("created_at", desc=False).execute()
        templates = r.data or []
    except Exception as e:
        logger.exception("templates_list_failed")
        raise HTTPException(status_code=500, detail=f"query failed: {str(e)[:200]}")

    # Optionally refresh approval status from Twilio
    for tmpl in templates:
        content_sid = tmpl.get("content_sid", "")
        if not content_sid or tmpl.get("status") == "approved":
            continue
        try:
            approval = await get_template_status(content_sid)
            # Twilio returns approval info; extract the status
            new_status = "pending"
            if isinstance(approval, dict):
                # The approval response has a "status" field in the approval_requests
                # or at top level depending on the Twilio API version
                new_status = (
                    approval.get("status")
                    or approval.get("approval_status")
                    or "pending"
                ).lower()
            if new_status in ("approved", "rejected") and new_status != tmpl.get("status"):
                tmpl["status"] = new_status
                try:
                    sb.table("broadcast_templates").update(
                        {"status": new_status}
                    ).eq("name", tmpl["name"]).execute()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("approval_refresh_failed sid=%s err=%s", content_sid, str(e)[:200])

    return {"templates": templates, "total": len(templates)}


@router.post("/campaigns")
async def create_campaign(request: Request, body: CampaignCreateRequest):
    """Create a new broadcast campaign."""
    _validate_cron_token(request)

    # Validate template exists in our definitions
    if body.template_name not in TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template: {body.template_name}. Available: {list(TEMPLATES.keys())}",
        )

    # Validate audience
    if body.audience not in VALID_AUDIENCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid audience: {body.audience}. Valid: {list(VALID_AUDIENCES)}",
        )

    # Parse scheduled_at
    try:
        scheduled_dt = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid scheduled_at format: {body.scheduled_at}")

    campaign_name = body.campaign_name or f"{body.template_name}_{body.audience}_{scheduled_dt.strftime('%Y%m%d')}"

    try:
        r = sb.table("broadcasts").insert({
            "campaign_name": campaign_name,
            "template_name": body.template_name,
            "audience_filter": {"audience": body.audience},
            "scheduled_at": scheduled_dt.isoformat(),
            "status": "scheduled",
            "total_sent": 0,
            "total_failed": 0,
        }).execute()
        campaign = (r.data or [{}])[0]
    except Exception as e:
        logger.exception("campaign_create_failed")
        raise HTTPException(status_code=500, detail=f"campaign create failed: {str(e)[:200]}")

    return {
        "campaign_id": campaign.get("id"),
        "campaign_name": campaign_name,
        "template_name": body.template_name,
        "audience": body.audience,
        "scheduled_at": scheduled_dt.isoformat(),
        "status": "scheduled",
    }


@router.get("/campaigns")
async def list_campaigns(request: Request):
    """List all campaigns with stats."""
    _validate_cron_token(request)

    try:
        r = sb.table("broadcasts").select("*").order("created_at", desc=True).execute()
        campaigns = r.data or []
    except Exception as e:
        logger.exception("campaigns_list_failed")
        raise HTTPException(status_code=500, detail=f"query failed: {str(e)[:200]}")

    return {"campaigns": campaigns, "total": len(campaigns)}


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(campaign_id: str, request: Request):
    """Manually trigger a campaign (useful for testing)."""
    _validate_cron_token(request)

    stats = await execute_campaign(campaign_id)
    return stats


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str, request: Request):
    """Cancel a scheduled campaign."""
    _validate_cron_token(request)

    try:
        r = sb.table("broadcasts").select("status").eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
    except Exception as e:
        logger.exception("campaign_cancel_fetch_failed id=%s", campaign_id)
        raise HTTPException(status_code=500, detail=f"fetch failed: {str(e)[:200]}")

    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")

    if campaign["status"] != "scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel campaign with status '{campaign['status']}'. Only 'scheduled' campaigns can be cancelled.",
        )

    try:
        sb.table("broadcasts").update({"status": "cancelled"}).eq("id", campaign_id).execute()
    except Exception as e:
        logger.exception("campaign_cancel_update_failed id=%s", campaign_id)
        raise HTTPException(status_code=500, detail=f"cancel failed: {str(e)[:200]}")

    return {"campaign_id": campaign_id, "status": "cancelled"}
