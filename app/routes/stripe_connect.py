"""Stripe Connect routes — onboarding, account status, checkout, webhooks."""
from __future__ import annotations

import logging
from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.stripe_connect import (
    create_express_account,
    create_onboarding_link,
    create_login_link,
    get_account_status,
    create_connect_payment_intent,
    refund_payment,
)

logger = logging.getLogger("stripe_connect_routes")

router = APIRouter(prefix="/v1/stripe-connect", tags=["stripe-connect"])


# ── Request models ────────────────────────────────────────────────


class CreateAccountReq(BaseModel):
    org_id: str
    email: str
    business_name: str = ""
    country: str = "US"


class OnboardingLinkReq(BaseModel):
    org_id: str
    stripe_account_id: str


class LoginLinkReq(BaseModel):
    stripe_account_id: str


class ConnectCheckoutReq(BaseModel):
    """Create a checkout for a ticket purchase through a connected account."""
    campaign_id: str
    lead_id: str
    price_cents: int  # e.g. 7900 = $79.00
    currency: str = "usd"
    tier: str = "VIP"
    fee_percent: float = 4.5  # platform fee %
    success_url: str = ""
    cancel_url: str = ""


class RefundReq(BaseModel):
    payment_intent_id: str
    amount_cents: int | None = None  # None = full refund
    reason: str = "requested_by_customer"


# ── Account endpoints ─────────────────────────────────────────────


@router.post("/accounts")
async def create_account(req: CreateAccountReq):
    """Create a new Express connected account for an org."""
    try:
        result = create_express_account(
            org_id=req.org_id,
            email=req.email,
            business_name=req.business_name,
            country=req.country,
        )
        return result
    except Exception as e:
        logger.exception("create_account_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@router.post("/accounts/onboarding-link")
async def get_onboarding_link(req: OnboardingLinkReq):
    """Generate a fresh onboarding link for an existing account."""
    try:
        url = create_onboarding_link(
            stripe_account_id=req.stripe_account_id,
            org_id=req.org_id,
        )
        return {"url": url}
    except Exception as e:
        logger.exception("onboarding_link_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@router.post("/accounts/login-link")
async def get_login_link(req: LoginLinkReq):
    """Generate an Express Dashboard login link."""
    try:
        url = create_login_link(stripe_account_id=req.stripe_account_id)
        return {"url": url}
    except Exception as e:
        logger.exception("login_link_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@router.get("/accounts/{stripe_account_id}/status")
async def account_status(stripe_account_id: str):
    """Get the current status of a connected account."""
    try:
        return get_account_status(stripe_account_id)
    except Exception as e:
        logger.exception("account_status_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Checkout / Payment ────────────────────────────────────────────


@router.post("/checkout")
async def connect_checkout(req: ConnectCheckoutReq):
    """Create a PaymentIntent for a ticket purchase via Connect.

    The platform collects a fee and transfers the rest to the organizer.
    """
    # Look up the campaign → org → stripe_account_id
    try:
        camp_res = sb.table("campaigns").select("org_id").eq("id", req.campaign_id).limit(1).execute()
        campaign = (camp_res.data or [None])[0]
        if not campaign:
            raise HTTPException(status_code=404, detail="campaign not found")

        org_id = campaign["org_id"]
        org_res = sb.table("orgs").select("stripe_account_id, stripe_account_status").eq("id", org_id).limit(1).execute()
        org = (org_res.data or [None])[0]
        if not org or not org.get("stripe_account_id"):
            raise HTTPException(status_code=400, detail="org has no connected Stripe account")
        if org.get("stripe_account_status") != "active":
            raise HTTPException(status_code=400, detail="org Stripe account is not active yet")

        stripe_account_id = org["stripe_account_id"]
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("lookup_org_stripe_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])

    # Calculate platform fee
    fee_cents = int(req.price_cents * (req.fee_percent / 100))

    try:
        result = create_connect_payment_intent(
            stripe_account_id=stripe_account_id,
            amount=req.price_cents,
            currency=req.currency,
            application_fee_amount=fee_cents,
            metadata={
                "campaign_id": req.campaign_id,
                "lead_id": req.lead_id,
                "tier": req.tier,
                "org_id": org_id,
                "platform": "event-ai-ops",
            },
        )
    except Exception as e:
        logger.exception("connect_checkout_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])

    # Record touchpoint
    try:
        sb.table("touchpoints").insert({
            "lead_id": req.lead_id,
            "channel": "stripe_connect",
            "event_type": "checkout_created",
            "payload": {
                "payment_intent_id": result["payment_intent_id"],
                "amount": req.price_cents,
                "currency": req.currency,
                "fee_cents": fee_cents,
                "tier": req.tier,
                "campaign_id": req.campaign_id,
                "org_id": org_id,
                "stripe_account_id": stripe_account_id,
            },
        }).execute()
    except Exception:
        pass

    return result


# ── Refunds ───────────────────────────────────────────────────────


@router.post("/refund")
async def connect_refund(req: RefundReq):
    """Refund a Connect payment (full or partial)."""
    try:
        result = refund_payment(
            payment_intent_id=req.payment_intent_id,
            amount=req.amount_cents,
            reason=req.reason,
        )
        return result
    except Exception as e:
        logger.exception("refund_failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ── Platform webhook ──────────────────────────────────────────────


@router.post("/platform-webhook")
async def platform_webhook(request: Request):
    """Handle Stripe Connect platform events (account.updated, etc.)."""
    if not settings.stripe_platform_secret_key:
        raise HTTPException(status_code=500, detail="Missing STRIPE_PLATFORM_SECRET_KEY")

    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")

    # If webhook secret is configured, verify signature
    if settings.stripe_platform_webhook_secret:
        try:
            event = stripe.Webhook.construct_event(
                raw, sig, settings.stripe_platform_webhook_secret
            )
        except Exception as e:
            logger.error("platform_webhook_sig_invalid %s", str(e)[:300])
            raise HTTPException(status_code=400, detail="invalid signature")
    else:
        import json
        event = json.loads(raw)

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    logger.info("stripe_connect_webhook type=%s account=%s", etype, obj.get("id", ""))

    if etype == "account.updated":
        acct_id = obj.get("id", "")
        charges_enabled = obj.get("charges_enabled", False)
        payouts_enabled = obj.get("payouts_enabled", False)
        details_submitted = obj.get("details_submitted", False)

        if charges_enabled and payouts_enabled:
            status = "active"
        elif details_submitted:
            status = "pending_verification"
        else:
            status = "onboarding"

        # Update org record
        try:
            sb.table("orgs").update({
                "stripe_account_status": status,
                "stripe_charges_enabled": charges_enabled,
                "stripe_payouts_enabled": payouts_enabled,
            }).eq("stripe_account_id", acct_id).execute()
            logger.info("org_stripe_status_updated account=%s status=%s", acct_id, status)
        except Exception:
            logger.exception("failed to update org stripe status for %s", acct_id)

    elif etype == "payment_intent.succeeded":
        # A Connect payment was successful
        meta = obj.get("metadata") or {}
        lead_id = meta.get("lead_id", "")
        campaign_id = meta.get("campaign_id", "")
        tier = meta.get("tier", "VIP")

        if lead_id:
            try:
                sb.table("leads").update({
                    "payment_status": "PAID",
                    "status": f"{tier}_PAID",
                }).eq("lead_id", lead_id).execute()
            except Exception:
                logger.exception("failed to mark lead %s as paid", lead_id)

            # Attribute commission to the agent who called this lead
            try:
                from ..services.commission_engine import attribute_sale
                await attribute_sale(lead_id, campaign_id)
                logger.info("connect_commission_attributed lead=%s campaign=%s", lead_id, campaign_id)
            except Exception:
                logger.exception("connect_commission_failed lead=%s", lead_id)

            try:
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "stripe_connect",
                    "event_type": "payment_succeeded",
                    "payload": {
                        "payment_intent_id": obj.get("id"),
                        "amount": obj.get("amount"),
                        "currency": obj.get("currency"),
                        "campaign_id": campaign_id,
                        "tier": tier,
                    },
                }).execute()
            except Exception:
                pass

    elif etype == "charge.refunded":
        meta = (obj.get("metadata") or {})
        lead_id = meta.get("lead_id", "")
        if lead_id:
            try:
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "stripe_connect",
                    "event_type": "payment_refunded",
                    "payload": {
                        "charge_id": obj.get("id"),
                        "amount_refunded": obj.get("amount_refunded"),
                    },
                }).execute()
            except Exception:
                pass

    return {"ok": True}
