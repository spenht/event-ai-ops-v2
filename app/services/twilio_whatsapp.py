from __future__ import annotations

import base64
import logging
from typing import Iterable, Optional
from urllib.parse import urlencode

import anyio
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


def _send_whatsapp_sync(
    url: str,
    data: list[tuple[str, str]],
    headers: dict[str, str],
    to_value: str,
    media_count: int,
) -> str:
    """Synchronous Twilio send — runs inside a worker thread."""
    # Encode as x-www-form-urlencoded manually because httpx sync Client
    # doesn't handle list-of-tuples the same way as AsyncClient.
    encoded = urlencode(data)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, content=encoded.encode("utf-8"), headers=headers)
        if r.status_code >= 400:
            logger.error("twilio_send_failed status=%s body=%s", r.status_code, r.text[:1200])
            r.raise_for_status()
        payload = r.json()
        sid = (payload.get("sid") or "").strip()
        logger.info("twilio_send_ok sid=%s to=%s media=%s", sid, to_value, media_count)
        return sid


async def send_whatsapp(
    to_e164: str,
    body: str,
    *,
    media_urls: Optional[Iterable[str]] = None,
    account_sid: str = "",
    auth_token: str = "",
    whatsapp_from: str = "",
) -> str:
    """Send WhatsApp message via Twilio REST API.

    Uses synchronous httpx.Client inside a thread to avoid async/sync
    conflicts with the event loop (known httpx issue).

    Accepts optional explicit Twilio credentials for multi-campaign support.
    Falls back to global settings if not provided.

    Returns Twilio Message SID.
    """

    sid = (account_sid or "").strip() or settings.twilio_account_sid
    token = (auth_token or "").strip() or settings.twilio_auth_token
    from_num = (whatsapp_from or "").strip() or settings.twilio_whatsapp_from

    if not sid or not token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    to_value = f"whatsapp:{to_e164}" if not to_e164.startswith("whatsapp:") else to_e164

    media_list = [u.strip() for u in (media_urls or []) if (u or "").strip()]

    data: list[tuple[str, str]] = [
        ("From", from_num),
        ("To", to_value),
        ("Body", body or ""),
    ]
    for u in media_list:
        data.append(("MediaUrl", u))

    headers = {"Authorization": _basic_auth_header(sid, token)}

    return await anyio.to_thread.run_sync(
        lambda: _send_whatsapp_sync(url, data, headers, to_value, len(media_list))
    )
