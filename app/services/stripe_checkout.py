from __future__ import annotations

import logging
from typing import Optional

import stripe

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("stripe_checkout")


def ensure_config() -> None:
    if not settings.stripe_secret_key:
        raise RuntimeError("Missing STRIPE_SECRET_KEY")
    if not settings.stripe_vip_price_id:
        raise RuntimeError("Missing STRIPE_VIP_PRICE_ID")
    if not settings.stripe_success_url or not settings.stripe_cancel_url:
        raise RuntimeError("Missing STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL")


async def create_vip_checkout_link(*, lead_id: str, event_id: str | None) -> Optional[str]:
    ensure_config()
    stripe.api_key = settings.stripe_secret_key

    lead_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
    lead = (lead_res.data or [None])[0]
    if not lead:
        return None

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": settings.stripe_vip_price_id, "quantity": 1}],
            success_url=settings.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=settings.stripe_cancel_url,
            customer_email=lead.get("email") or None,
            metadata={
                "lead_id": lead_id,
                "event_id": event_id or "",
                "tier": "VIP",
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
                "payload": {"session_id": session.id, "url": session.url, "tier": "VIP", "event_id": event_id},
            }
        ).execute()
    except Exception:
        pass

    return session.url
