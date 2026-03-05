"""Lightweight URL shortener backed by Supabase."""
from __future__ import annotations

import secrets
import logging
from typing import Optional

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("url_shortener")


def _random_code(prefix: str = "", length: int = 8) -> str:
    """Generate a short random code like 'cal_a8f3b2c1'."""
    raw = secrets.token_urlsafe(length)[:length]
    return f"{prefix}{raw}" if prefix else raw


async def create_short_url(
    long_url: str,
    *,
    lead_id: str = "",
    url_type: str = "generic",
    prefix: str = "",
) -> str:
    """Create a short URL and return the full redirect URL.

    Returns the original long_url as fallback if shortening fails.
    """
    if not long_url:
        return long_url

    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        return long_url  # Can't shorten without a base URL

    code = _random_code(prefix=prefix, length=8)

    try:
        sb.table("short_urls").insert({
            "code": code,
            "long_url": long_url,
            "lead_id": lead_id or None,
            "url_type": url_type,
        }).execute()
        return f"{base}/v1/s/{code}"
    except Exception as exc:
        logger.error("short_url_create_failed code=%s err=%s", code, str(exc)[:200])
        return long_url  # Graceful fallback — never break the flow
