from __future__ import annotations

import logging
from typing import Optional

import stripe

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("stripe_checkout")


def _resolve_price_id(option: int = 1, campaign: dict | None = None) -> str:
    """Return the Stripe price ID for the given VIP option.

    option=1  ->  1 VIP individual (79 USD)
    option=2  ->  2 VIPs promo (97 USD)

    Checks campaign.stripe_price_ids first, then falls back to global settings.
    Campaign keys can be "vip_1"/"vip_2" or just "1"/"2".
    """
    price_ids = (campaign or {}).get("stripe_price_ids") or {}
    if option == 2:
        cid = price_ids.get("vip_2") or price_ids.get("2") or ""
        if cid:
            return cid
        if settings.stripe_vip_price_id_2:
            return settings.stripe_vip_price_id_2
    if option == 1:
        cid = price_ids.get("vip_1") or price_ids.get("1") or ""
        if cid:
            return cid
        if settings.stripe_vip_price_id_1:
            return settings.stripe_vip_price_id_1
    # Legacy fallback
    return settings.stripe_vip_price_id


def _resolve_stripe_key(campaign: dict | None = None) -> str:
    """Return the Stripe secret key, preferring campaign-level override."""
    key = (campaign or {}).get("stripe_secret_key") or ""
    return key.strip() if key else settings.stripe_secret_key


def _resolve_urls(campaign: dict | None = None) -> tuple[str, str]:
    """Return (success_url, cancel_url), preferring campaign-level overrides."""
    c = campaign or {}
    success = (c.get("stripe_success_url") or "").strip() or settings.stripe_success_url
    cancel = (c.get("stripe_cancel_url") or "").strip() or settings.stripe_cancel_url
    return success, cancel


def ensure_config(*, option: int = 1, campaign: dict | None = None) -> None:
    if not _resolve_stripe_key(campaign):
        raise RuntimeError("Missing STRIPE_SECRET_KEY")
    if not _resolve_price_id(option, campaign):
        raise RuntimeError("Missing STRIPE_VIP_PRICE_ID")
    success, cancel = _resolve_urls(campaign)
    if not success or not cancel:
        raise RuntimeError("Missing STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL")


async def create_vip_checkout_link(
    *, lead_id: str, event_id: str | None, option: int = 1, campaign: dict | None = None
) -> Optional[str]:
    """Create a Stripe Checkout session for VIP.

    option=1  ->  1 VIP individual (79 USD)
    option=2  ->  2 VIPs promo (97 USD)
    """
    ensure_config(option=option, campaign=campaign)
    stripe.api_key = _resolve_stripe_key(campaign)
    price_id = _resolve_price_id(option, campaign)

    lead_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
    lead = (lead_res.data or [None])[0]
    if not lead:
        return None

    success_url, cancel_url = _resolve_urls(campaign)
    label = "1 VIP" if option == 1 else "2 VIPs promo"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            customer_email=lead.get("email") or None,
            metadata={
                "lead_id": lead_id,
                "event_id": event_id or "",
                "campaign_id": (campaign or {}).get("id") or "",
                "tier": "VIP",
                "option": str(option),
                "label": label,
                "whatsapp": lead.get("whatsapp") or "",
            },
        )
    except Exception as e:
        logger.exception("stripe_create_session_failed %s", str(e)[:200])
        return None

    try:
        sb.table("touchpoints").insert(
            {
                "lead_id": lead_id,
                "channel": "stripe",
                "event_type": "checkout_created",
                "payload": {
                    "session_id": session.id,
                    "url": session.url,
                    "tier": "VIP",
                    "option": option,
                    "label": label,
                    "event_id": event_id,
                },
            }
        ).execute()
    except Exception:
        pass

    return session.url
