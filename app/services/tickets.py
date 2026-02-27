from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import qrcode
from PIL import Image, ImageDraw, ImageFont

from ..deps import sb

TICKETS_DIR = Path(os.getenv("TICKETS_DIR", "/tmp/tickets"))
TICKETS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_text(s: str, max_len: int = 60) -> str:
    s = (s or "").strip()
    return s[:max_len]


def _ticket_id() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")


def _ticket_token() -> str:
    return secrets.token_urlsafe(24)


def _make_qr(payload: str, *, box_size: int = 10, border: int = 2) -> Image.Image:
    qr = qrcode.QRCode(box_size=box_size, border=border)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.convert("RGB")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Pillow default font always available
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def generate_ticket_png(*, lead: dict[str, Any], tier: str, event: dict[str, Any]) -> dict[str, str]:
    """Generate a ticket image and store a mapping in touchpoints.

    Returns {ticket_id, token, file_path}
    """
    tid = _ticket_id()
    tok = _ticket_token()

    name = _safe_text(lead.get("name") or "Participante")
    email = _safe_text(lead.get("email") or "")
    event_name = _safe_text(event.get("event_name") or "Evento")
    date = _safe_text(event.get("event_date") or "")
    place = _safe_text(event.get("event_place") or "")

    # Stable code for scanning (human-friendly)
    raw = f"{lead.get('lead_id')}|{tid}|{tier}|{email}".encode("utf-8")
    code = hashlib.sha256(raw).hexdigest()[:10].upper()

    qr_payload = {
        "ticket_id": tid,
        "code": code,
        "tier": tier,
        "lead_id": lead.get("lead_id"),
        "email": lead.get("email"),
    }

    qr_img = _make_qr(str(qr_payload))

    # Create canvas
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    title_font = _load_font(58)
    big_font = _load_font(52)
    body_font = _load_font(34)
    small_font = _load_font(28)

    y = 70
    draw.text((70, y), event_name, fill="black", font=title_font)
    y += 90

    badge = f"BOLETO {tier.upper()}"
    draw.text((70, y), badge, fill="black", font=big_font)
    y += 90

    draw.text((70, y), f"Nombre: {name}", fill="black", font=body_font)
    y += 55
    if email:
        draw.text((70, y), f"Correo: {email}", fill="black", font=body_font)
        y += 55

    if date:
        draw.text((70, y), f"Fecha: {date}", fill="black", font=body_font)
        y += 55
    if place:
        draw.text((70, y), f"Lugar: {place}", fill="black", font=body_font)
        y += 55

    y += 25
    draw.text((70, y), f"Código: {code}", fill="black", font=body_font)

    # Paste QR
    qr_size = 520
    qr_img = qr_img.resize((qr_size, qr_size))
    img.paste(qr_img, (W - qr_size - 90, H - qr_size - 220))

    draw.text((70, H - 170), "Muestra este QR en la entrada.", fill="black", font=small_font)
    draw.text((70, H - 130), f"Generado: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", fill="black", font=small_font)

    fp = TICKETS_DIR / f"{tid}.png"
    img.save(fp, format="PNG", optimize=True)

    # Persist mapping for lookup (uses existing table)
    try:
        sb.table("touchpoints").insert(
            {
                "lead_id": lead.get("lead_id"),
                "channel": "whatsapp",
                "event_type": "ticket_created",
                "payload": {"ticket_id": tid, "token": tok, "tier": tier, "code": code, "file": str(fp)},
            }
        ).execute()
    except Exception:
        # If DB insert fails, still return file so flow can continue.
        pass

    return {"ticket_id": tid, "token": tok, "file_path": str(fp)}


def lookup_ticket(ticket_id: str) -> Optional[dict[str, str]]:
    try:
        r = (
            sb.table("touchpoints")
            .select("payload")
            .eq("event_type", "ticket_created")
            .contains("payload", {"ticket_id": ticket_id})
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        row = (r.data or [None])[0]
        if not row:
            return None
        p = row.get("payload") or {}
        return {
            "token": (p.get("token") or ""),
            "file": (p.get("file") or ""),
            "tier": (p.get("tier") or ""),
            "code": (p.get("code") or ""),
        }
    except Exception:
        return None
