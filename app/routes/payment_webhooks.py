"""Payment Webhooks — auto-record Stripe + Whop payments as they happen."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from ..deps import sb

logger = logging.getLogger("payment_webhooks")
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/stripe/{account_key}")
async def stripe_webhook(account_key: str, request: Request):
    body = await request.body()
    # In production, verify signature with webhook secret
    # For now, just parse the event
    try:
        event = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, 400)

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "payment_intent.succeeded":
        pi = data
        amount = pi.get("amount", 0) / 100
        currency = pi.get("currency", "usd").upper()
        meta = pi.get("metadata", {})

        txn = {
            "external_id": pi["id"],
            "source": f"stripe_{account_key}",
            "type": "sale",
            "amount": amount,
            "currency": currency,
            "txn_date": datetime.fromtimestamp(
                pi.get("created", 0), tz=timezone.utc
            ).isoformat(),
            "description": pi.get("description") or "Stripe payment",
            "counterparty": pi.get("receipt_email")
            or meta.get("customer_email")
            or "",
            "project_id": meta.get("project_id") or None,
            "auto_assigned": bool(meta.get("project_id")),
            "metadata": {
                "source": "webhook",
                "agent_id": meta.get("agent_id", ""),
                "product_id": meta.get("product_id", ""),
                "product_name": meta.get("product_name", ""),
                "gateway_key": account_key,
            },
        }
        try:
            sb.table("financial_transactions").upsert(
                txn, on_conflict="external_id,source"
            ).execute()
            logger.info(
                "webhook_stripe_recorded pi=%s amount=%s %s",
                pi["id"],
                amount,
                currency,
            )
        except Exception as e:
            logger.warning("webhook_stripe_upsert_error: %s", str(e)[:100])

    elif event_type == "charge.refunded":
        charge = data
        amount = charge.get("amount_refunded", 0) / 100
        txn = {
            "external_id": f"refund_{charge['id']}",
            "source": f"stripe_{account_key}",
            "type": "refund",
            "amount": amount,
            "currency": charge.get("currency", "usd").upper(),
            "txn_date": datetime.now(timezone.utc).isoformat(),
            "description": f"Refund: {charge.get('description', '')}",
            "counterparty": charge.get("billing_details", {}).get("name", ""),
            "metadata": {"source": "webhook", "original_charge": charge["id"]},
        }
        try:
            sb.table("financial_transactions").upsert(
                txn, on_conflict="external_id,source"
            ).execute()
        except Exception:
            pass

    return {"received": True}


@router.post("/whop")
async def whop_webhook(request: Request):
    body = await request.body()
    try:
        event = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, 400)

    event_type = event.get("event", event.get("type", ""))
    data = event.get("data", event)

    if event_type in ("payment.completed", "payment.succeeded"):
        payment = data.get("payment", data)
        amount = payment.get("subtotal") or payment.get("final_amount") or 0
        if amount > 10000:
            amount = amount / 100

        txn = {
            "external_id": payment.get("id", ""),
            "source": "whop",
            "type": "sale",
            "amount": amount,
            "currency": (payment.get("currency") or "USD").upper(),
            "txn_date": payment.get("created_at")
            or datetime.now(timezone.utc).isoformat(),
            "description": payment.get("product_name") or "Whop payment",
            "counterparty": payment.get("user_email") or "",
            "metadata": {
                "source": "webhook",
                "product_id": payment.get("product_id", ""),
                "membership_id": payment.get("membership_id", ""),
            },
        }
        try:
            sb.table("financial_transactions").upsert(
                txn, on_conflict="external_id,source"
            ).execute()
            logger.info(
                "webhook_whop_recorded id=%s amount=%s",
                payment.get("id"),
                amount,
            )
        except Exception as e:
            logger.warning("webhook_whop_error: %s", str(e)[:100])

    return {"received": True}
