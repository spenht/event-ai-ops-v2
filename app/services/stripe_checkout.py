from __future__ import annotations

import logging
from typing import Optional

import stripe

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("stripe_checkout")


def _resolve_price_id(option: int = 1) -> str:
    """Return the Stripe price ID for the given VIP option.

    option=1  ->  1 VIP individual (79 USD)
    option=2  ->  2 VIPs promo (97 USD)
    Falls back to the legacy STRIPE_VIP_PRICE_ID if the per-option IDs are missing.
    """
    if option == 2 and settings.stripe_vip_price_id_2:
        return settings.stripe_vip_price_id_2
    if option == 1 and settings.stripe_vip_price_id_1:
        return settings.stripe_vip_price_id_1
    # Legacy fallback
    return settings.stripe_vip_price_id


def ensure_config(*, option: int = 1) -> None:
    if not settings.stripe_secret_key:
        raise RuntimeError("Missing STRIPE_SECRET_KEY")
    if not _resolve_price_id(option):
        raise RuntimeError("Missing STRIPE_VIP_PRICE_ID")
    if not settings.stripe_success_url or not settings.stripe_cancel_url:
        raise RuntimeError("Missing STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL")


async def create_vip_checkout_link(
    *, lead_id: str, event_id: str | None, option: int = 1
) -> Optional[str]:
    """Create a Stripe Checkout session for VIP.

    option=1  ->  1 VIP individual (79 USD)
    option=2  ->  2 VIPs promo (97 USD)
    """
    ensure_config(option=option)
    stripe.api_key = settings.stripe_secret_key
    price_id = _resolve_price_id(option)

    lead_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
    lead = (lead_res.data or [None])[0]
    if not lead:
        return None

    label = "1 VIP" if option == 1 else "2 VIPs promo"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=settings.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=settings.stripe_cancel_url,
            customer_email=lead.get("email") or None,
            metadata={
                "lead_id": lead_id,
                "event_id": event_id or "",
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
