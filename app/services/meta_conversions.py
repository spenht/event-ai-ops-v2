"""Meta Conversions API (CAPI) integration for ad conversion tracking.

Sends Lead and Purchase events to Meta so ads can optimise delivery.
Uses httpx synchronous client inside anyio.to_thread.run_sync() to avoid
blocking the event loop (same pattern as google_sheets.py).

If META_PIXEL_ID or META_CONVERSIONS_API_TOKEN is not set, all calls
silently no-op (graceful degradation).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Optional

import anyio
import httpx

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("meta_conversions")

GRAPH_API_VERSION = "v21.0"


# ── hashing helpers ───────────────────────────────────────────────


def _sha256(value: str) -> str:
    """SHA256 hash a value per Meta CAPI spec (lowercase, stripped)."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _normalize_phone(phone: str) -> str:
    """Normalize phone to digits-only with country code (Meta CAPI format)."""
    return re.sub(r"\D", "", phone)


def _build_user_data(lead: dict[str, Any]) -> dict[str, Any]:
    """Build hashed user_data from lead dict for Meta CAPI."""
    ud: dict[str, Any] = {}

    phone = (lead.get("whatsapp") or lead.get("phone") or "").strip()
    if phone:
        ud["ph"] = [_sha256(_normalize_phone(phone))]

    email = (lead.get("email") or "").strip()
    if email:
        ud["em"] = [_sha256(email)]

    name = (lead.get("name") or "").strip()
    if name:
        parts = name.split()
        if parts:
            ud["fn"] = [_sha256(parts[0])]
        if len(parts) > 1:
            ud["ln"] = [_sha256(" ".join(parts[1:]))]

    country = (lead.get("country") or "").strip()
    if country:
        ud["country"] = [_sha256(country.lower()[:2])]

    return ud


# ── sync send (runs in worker thread) ────────────────────────────


def _send_event_sync(
    event_name: str,
    lead: dict[str, Any],
    custom_data: Optional[dict[str, Any]],
    event_id: str,
) -> None:
    """Send a single event to Meta CAPI (synchronous). Runs in worker thread."""
    pixel_id = settings.meta_pixel_id
    token = settings.meta_conversions_api_token
    if not pixel_id or not token:
        return

    user_data = _build_user_data(lead)
    if not user_data:
        logger.warning("meta_capi_skip no_user_data lead=%s", lead.get("lead_id"))
        return

    event_time = int(time.time())

    payload: dict[str, Any] = {
        "event_name": event_name,
        "event_time": event_time,
        "action_source": "other",
        "event_id": event_id,
        "user_data": user_data,
    }
    if custom_data:
        payload["custom_data"] = custom_data

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{pixel_id}/events"

    status_code = 0
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                url,
                params={"access_token": token},
                json={"data": [payload]},
            )
            status_code = r.status_code
            if r.status_code >= 400:
                logger.error(
                    "meta_capi_failed event=%s status=%s body=%s",
                    event_name,
                    r.status_code,
                    r.text[:500],
                )
            else:
                logger.info(
                    "meta_capi_sent event=%s lead=%s event_id=%s",
                    event_name,
                    lead.get("lead_id"),
                    event_id,
                )
    except Exception as exc:
        logger.error("meta_capi_http_error event=%s err=%s", event_name, str(exc)[:300])

    # Log touchpoint
    lead_id = lead.get("lead_id") or ""
    if lead_id:
        try:
            sb.table("touchpoints").insert({
                "lead_id": lead_id,
                "channel": "meta",
                "event_type": "capi_event_sent",
                "payload": {
                    "event_name": event_name,
                    "event_id": event_id,
                    "status": status_code,
                },
            }).execute()
        except Exception:
            pass


# ── public async wrappers (fire-and-forget safe) ─────────────────


async def send_lead_event(lead: dict[str, Any]) -> None:
    """Fire-and-forget: send Lead event to Meta CAPI.

    Safe to call via asyncio.create_task(). Never raises.
    """
    if not settings.meta_pixel_id or not settings.meta_conversions_api_token:
        return
    try:
        lead_id = lead.get("lead_id") or ""
        event_id = f"{lead_id}_Lead_{int(time.time())}"
        await anyio.to_thread.run_sync(
            lambda: _send_event_sync("Lead", lead, None, event_id)
        )
    except Exception as exc:
        logger.error("meta_capi_lead_failed err=%s", str(exc)[:200])


async def send_purchase_event(
    lead: dict[str, Any],
    currency: str = "USD",
    value: float = 0.0,
) -> None:
    """Fire-and-forget: send Purchase event to Meta CAPI.

    Safe to call via asyncio.create_task(). Never raises.
    """
    if not settings.meta_pixel_id or not settings.meta_conversions_api_token:
        return
    try:
        lead_id = lead.get("lead_id") or ""
        event_id = f"{lead_id}_Purchase_{int(time.time())}"
        custom_data: dict[str, Any] = {"currency": currency, "value": value}
        await anyio.to_thread.run_sync(
            lambda: _send_event_sync("Purchase", lead, custom_data, event_id)
        )
    except Exception as exc:
        logger.error("meta_capi_purchase_failed err=%s", str(exc)[:200])
