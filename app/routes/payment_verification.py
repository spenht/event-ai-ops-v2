from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.commission_engine import attribute_sale

logger = logging.getLogger("payment_verification")

router = APIRouter(prefix="/v1/payments/verifications", tags=["payment-verifications"])


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


class SubmitVerificationBody(BaseModel):
    campaign_id: str
    lead_id: str
    agent_id: str
    payment_method: str = "cash"  # cash, transfer, deposit, other
    amount: float = 0
    proof_url: Optional[str] = None
    notes: str = ""


class UpdateVerificationBody(BaseModel):
    campaign_id: str
    status: str  # "approved", "rejected"
    admin_notes: str = ""


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/")
async def list_verifications(
    request: Request,
    campaign_id: str,
    status: Optional[str] = None,
):
    """List payment verifications with optional status filter."""
    _validate_auth(request, campaign_id)
    try:
        q = (
            sb.table("payment_verifications")
            .select("*")
            .eq("campaign_id", campaign_id)
        )
        if status:
            q = q.eq("status", status)
        r = q.order("created_at", desc=True).execute()
        return {"ok": True, "data": r.data or []}
    except Exception as exc:
        logger.error("list_verifications_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.post("/")
async def submit_verification(request: Request, body: SubmitVerificationBody):
    """Submit a new payment verification (agent reports manual payment)."""
    _validate_auth(request, body.campaign_id)
    try:
        record = {
            "campaign_id": body.campaign_id,
            "lead_id": body.lead_id,
            "agent_id": body.agent_id,
            "payment_method": body.payment_method,
            "amount": body.amount,
            "proof_url": body.proof_url,
            "notes": body.notes,
            "status": "pending",
        }
        r = sb.table("payment_verifications").insert(record).execute()
        created = (r.data or [None])[0]
        logger.info(
            "verification_submitted campaign=%s lead=%s agent=%s method=%s",
            body.campaign_id, body.lead_id, body.agent_id, body.payment_method,
        )
        return {"ok": True, "data": created}
    except Exception as exc:
        logger.error(
            "submit_verification_failed campaign=%s lead=%s err=%s",
            body.campaign_id, body.lead_id, str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.patch("/{verification_id}")
async def update_verification(
    request: Request,
    verification_id: str,
    body: UpdateVerificationBody,
):
    """Update verification status (admin approves/rejects)."""
    _validate_auth(request, body.campaign_id)
    try:
        now = datetime.now(timezone.utc).isoformat()

        # Fetch current verification
        existing = (
            sb.table("payment_verifications")
            .select("*")
            .eq("id", verification_id)
            .eq("campaign_id", body.campaign_id)
            .limit(1)
            .execute()
        )
        verification = (existing.data or [None])[0]
        if not verification:
            raise HTTPException(status_code=404, detail="verification not found")

        # Update status
        update_data: dict[str, Any] = {
            "status": body.status,
            "admin_notes": body.admin_notes,
            "reviewed_at": now,
            "updated_at": now,
        }
        r = (
            sb.table("payment_verifications")
            .update(update_data)
            .eq("id", verification_id)
            .execute()
        )
        updated = (r.data or [None])[0]

        # If approved, update lead payment_status and trigger commission
        if body.status == "approved":
            lead_id = verification["lead_id"]
            campaign_id = body.campaign_id

            # Update lead payment_status to PAID
            try:
                sb.table("leads").update({
                    "payment_status": "PAID",
                    "updated_at": now,
                }).eq("lead_id", lead_id).eq("campaign_id", campaign_id).execute()
                logger.info(
                    "lead_marked_paid via verification lead=%s campaign=%s",
                    lead_id, campaign_id,
                )
            except Exception as exc_lead:
                logger.error(
                    "lead_update_failed lead=%s err=%s",
                    lead_id, str(exc_lead)[:300],
                )

            # Trigger commission attribution
            try:
                commission = await attribute_sale(lead_id, campaign_id)
                if commission:
                    logger.info(
                        "commission_attributed_via_verification lead=%s commission_id=%s",
                        lead_id, commission.get("id"),
                    )
            except Exception as exc_comm:
                logger.error(
                    "commission_attribution_failed lead=%s err=%s",
                    lead_id, str(exc_comm)[:300],
                )

        return {"ok": True, "data": updated}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "update_verification_failed id=%s err=%s",
            verification_id, str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.get("/all-payments")
async def all_payments(request: Request, campaign_id: str):
    """Admin view: all PAID leads with Stripe touchpoints and verifications."""
    _validate_auth(request, campaign_id)
    try:
        # Get all PAID leads
        paid_leads = (
            sb.table("leads")
            .select("lead_id, name, phone, email, payment_status, tier_interest, whatsapp, last_contact_at")
            .eq("campaign_id", campaign_id)
            .eq("payment_status", "PAID")
            .order("last_contact_at", desc=True)
            .execute()
        )

        # Get all verifications for this campaign
        verifications = (
            sb.table("payment_verifications")
            .select("*")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        verification_map: dict[str, list] = {}
        for v in (verifications.data or []):
            lid = v.get("lead_id")
            if lid not in verification_map:
                verification_map[lid] = []
            verification_map[lid].append(v)

        # Get commissions
        commissions = (
            sb.table("commissions")
            .select("*")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        commission_map: dict[str, dict] = {}
        for c in (commissions.data or []):
            commission_map[c.get("lead_id", "")] = c

        # Combine
        results = []
        for lead in (paid_leads.data or []):
            lid = lead["lead_id"]
            results.append({
                "lead": lead,
                "verifications": verification_map.get(lid, []),
                "commission": commission_map.get(lid),
            })

        return {"ok": True, "data": results, "total": len(results)}

    except Exception as exc:
        logger.error("all_payments_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])
