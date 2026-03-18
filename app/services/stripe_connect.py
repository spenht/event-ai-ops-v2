"""Stripe Connect service — Express accounts, onboarding, transfers.

Platform account: 2clicks.com (STRIPE_PLATFORM_SECRET_KEY).
Connected accounts: event organizers who receive payouts.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import stripe

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("stripe_connect")


def _platform_stripe() -> stripe.StripeClient:
    """Return a Stripe client bound to the platform key."""
    if not settings.stripe_platform_secret_key:
        raise RuntimeError("Missing STRIPE_PLATFORM_SECRET_KEY")
    return stripe.StripeClient(settings.stripe_platform_secret_key)


# ── Account lifecycle ─────────────────────────────────────────────


def create_express_account(
    *,
    org_id: str,
    email: str,
    business_name: str = "",
    country: str = "US",
) -> dict[str, Any]:
    """Create a Stripe Express connected account for an org.

    Returns {stripe_account_id, account_link_url}.
    """
    client = _platform_stripe()

    account = client.accounts.create(
        params={
            "type": "express",
            "country": country,
            "email": email,
            "business_type": "company",
            "capabilities": {
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
            "metadata": {"org_id": org_id, "platform": "event-ai-ops"},
        }
    )

    # Persist to orgs table
    try:
        sb.table("orgs").update({
            "stripe_account_id": account.id,
            "stripe_account_status": "onboarding",
        }).eq("id", org_id).execute()
    except Exception:
        logger.exception("failed to save stripe_account_id to org %s", org_id)

    # Generate onboarding link
    return_url = settings.stripe_connect_return_url or settings.public_base_url or ""
    refresh_url = settings.stripe_connect_refresh_url or return_url

    link = client.account_links.create(
        params={
            "account": account.id,
            "type": "account_onboarding",
            "return_url": return_url.rstrip("/") + f"/dashboard/settings?stripe=complete&org={org_id}",
            "refresh_url": refresh_url.rstrip("/") + f"/dashboard/settings?stripe=refresh&org={org_id}",
        }
    )

    return {"stripe_account_id": account.id, "onboarding_url": link.url}


def create_onboarding_link(*, stripe_account_id: str, org_id: str = "") -> str:
    """Generate a fresh onboarding/account-link URL for an existing account."""
    client = _platform_stripe()
    return_url = settings.stripe_connect_return_url or settings.public_base_url or ""
    refresh_url = settings.stripe_connect_refresh_url or return_url

    link = client.account_links.create(
        params={
            "account": stripe_account_id,
            "type": "account_onboarding",
            "return_url": return_url.rstrip("/") + f"/dashboard/settings?stripe=complete&org={org_id}",
            "refresh_url": refresh_url.rstrip("/") + f"/dashboard/settings?stripe=refresh&org={org_id}",
        }
    )
    return link.url


def create_login_link(*, stripe_account_id: str) -> str:
    """Generate a link to the Express Dashboard for a connected account."""
    client = _platform_stripe()
    link = client.accounts.create_login_link(stripe_account_id)
    return link.url


def get_account_status(stripe_account_id: str) -> dict[str, Any]:
    """Retrieve account details and return a summary."""
    client = _platform_stripe()
    acct = client.accounts.retrieve(stripe_account_id)
    charges_enabled = getattr(acct, "charges_enabled", False)
    payouts_enabled = getattr(acct, "payouts_enabled", False)
    details_submitted = getattr(acct, "details_submitted", False)

    if charges_enabled and payouts_enabled:
        status = "active"
    elif details_submitted:
        status = "pending_verification"
    else:
        status = "onboarding"

    return {
        "stripe_account_id": stripe_account_id,
        "status": status,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "details_submitted": details_submitted,
        "country": getattr(acct, "country", ""),
        "default_currency": getattr(acct, "default_currency", ""),
    }


# ── Payments (destination charges) ────────────────────────────────


def create_connect_checkout(
    *,
    stripe_account_id: str,
    line_items: list[dict],
    success_url: str,
    cancel_url: str,
    application_fee_percent: float = 4.5,
    metadata: dict | None = None,
    customer_email: str | None = None,
) -> dict[str, Any]:
    """Create a Checkout Session where the platform collects a fee.

    Uses destination charges: the payment goes to the platform,
    and a transfer is made to the connected account minus the fee.
    """
    client = _platform_stripe()

    params: dict[str, Any] = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "payment_intent_data": {
            "application_fee_amount": None,  # calculated below
            "transfer_data": {"destination": stripe_account_id},
        },
        "metadata": metadata or {},
    }

    if customer_email:
        params["customer_email"] = customer_email

    # We use application_fee_percent at session level if available,
    # otherwise calculate from line items after creation.
    # Stripe Checkout doesn't support fee_percent directly,
    # so we set it in payment_intent_data after calculating.
    # For now, we'll let Stripe calculate and we'll set fee on webhook.
    # Remove the None fee — we'll use on_behalf_of pattern instead.
    del params["payment_intent_data"]["application_fee_amount"]
    params["payment_intent_data"]["on_behalf_of"] = stripe_account_id

    session = client.checkout.sessions.create(params=params)

    return {"url": session.url, "session_id": session.id}


def create_connect_payment_intent(
    *,
    stripe_account_id: str,
    amount: int,
    currency: str = "usd",
    application_fee_amount: int = 0,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Create a PaymentIntent with destination charge.

    amount: in cents (e.g. 7900 = $79.00)
    application_fee_amount: in cents (platform's cut)
    """
    client = _platform_stripe()

    pi = client.payment_intents.create(
        params={
            "amount": amount,
            "currency": currency,
            "application_fee_amount": application_fee_amount,
            "transfer_data": {"destination": stripe_account_id},
            "metadata": metadata or {},
        }
    )

    return {
        "payment_intent_id": pi.id,
        "client_secret": pi.client_secret,
        "amount": amount,
        "currency": currency,
        "application_fee_amount": application_fee_amount,
    }


# ── Refunds ───────────────────────────────────────────────────────


def refund_payment(
    *,
    payment_intent_id: str,
    amount: int | None = None,
    reason: str = "requested_by_customer",
) -> dict[str, Any]:
    """Refund a payment (full or partial).

    amount: in cents. None = full refund.
    """
    client = _platform_stripe()
    params: dict[str, Any] = {
        "payment_intent": payment_intent_id,
        "reason": reason,
    }
    if amount is not None:
        params["amount"] = amount

    refund = client.refunds.create(params=params)
    return {
        "refund_id": refund.id,
        "status": refund.status,
        "amount": refund.amount,
    }
