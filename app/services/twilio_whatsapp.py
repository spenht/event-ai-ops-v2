from __future__ import annotations

import base64
import logging
from typing import Iterable, Optional

import httpx

from ..settings import settings

logger = logging.getLogger("twilio_whatsapp")


def _basic_auth_header(account_sid: str, auth_token: str) -> str:
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


def normalize_mx_whatsapp(raw: str) -> str:
    """Normalize WhatsApp E.164 (handles +521 vs +52)."""
    s = (raw or "").strip()
    if s.startswith("whatsapp:"):
        s = s.replace("whatsapp:", "")
    return s


async def send_whatsapp(
    to_e164: str,
    body: str,
    *,
    media_urls: Optional[Iterable[str]] = None,
) -> str:
    """Send WhatsApp message via Twilio REST API.

    Twilio expects MediaUrl to be repeated (same key multiple times), so we send
    form data as list-of-tuples.

    Returns Twilio Message SID.
    """

    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    to_value = f"whatsapp:{to_e164}" if not to_e164.startswith("whatsapp:") else to_e164

    media_list = [u.strip() for u in (media_urls or []) if (u or "").strip()]

    data: list[tuple[str, str]] = [
        ("From", settings.twilio_whatsapp_from),
        ("To", to_value),
        ("Body", body or ""),
    ]
    for u in media_list:
        data.append(("MediaUrl", u))

    headers = {"Authorization": _basic_auth_header(settings.twilio_account_sid, settings.twilio_auth_token)}

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, data=data, headers=headers)
        if r.status_code >= 400:
            logger.error("twilio_send_failed status=%s body=%s", r.status_code, r.text[:1200])
            r.raise_for_status()
        payload = r.json()
        sid = (payload.get("sid") or "").strip()
        logger.info("twilio_send_ok sid=%s to=%s media=%s", sid, to_value, len(media_list))
        return sid
