from __future__ import annotations

import hashlib
import math
import os
import random
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..deps import sb

TICKETS_DIR = Path(os.getenv("TICKETS_DIR", "/tmp/tickets"))
TICKETS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Canvas constants
# ---------------------------------------------------------------------------
WIDTH, HEIGHT = 1080, 1920  # 9:16 smartphone ratio
LEFT_MARGIN = 90
CENTER_X = WIDTH // 2

# ---------------------------------------------------------------------------
# Font helpers — works on both macOS (HelveticaNeue) and Linux (DejaVu)
# ---------------------------------------------------------------------------
_FONT_PATHS = {
    "bold": [
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}

# HelveticaNeue.ttc indices: 0=Regular, 1=Bold, 7=Light, 10=Medium
_HN_INDEX = {"bold": 1, "medium": 10, "light": 7, "regular": 0}


def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Load a font at the desired weight with cross-platform fallbacks."""
    family = "bold" if weight in ("bold", "condensed_bold") else "regular"
    for path in _FONT_PATHS[family]:
        try:
            if path.endswith(".ttc"):
                return ImageFont.truetype(path, size, index=_HN_INDEX.get(weight, 0))
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_text(s: str, max_len: int = 60) -> str:
    return (s or "").strip()[:max_len]


def _ticket_id() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")


def _ticket_token() -> str:
    return secrets.token_urlsafe(24)


def _make_qr(payload: str, *, box_size: int = 10, border: int = 2) -> Image.Image:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.convert("RGBA")


# ---------------------------------------------------------------------------
# Color utilities
# ---------------------------------------------------------------------------

def _lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(len(c1)))


# ---------------------------------------------------------------------------
# Background renderers
# ---------------------------------------------------------------------------

def _draw_vip_background(img):
    draw = ImageDraw.Draw(img)
    w, h = img.size

    for y in range(h):
        t = y / h
        c = _lerp_color((26, 16, 0), (10, 8, 5), t)
        draw.line([(0, y), (w, y)], fill=c)

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    cx, cy = w // 2, int(h * 0.22)

    for rx, ry, rgba in [
        (800, 700, (255, 180, 40, 7)),
        (650, 560, (255, 170, 30, 10)),
        (500, 440, (255, 165, 25, 14)),
        (380, 340, (255, 160, 20, 18)),
        (280, 250, (255, 155, 15, 22)),
        (200, 180, (255, 150, 10, 28)),
        (130, 120, (255, 145, 5, 32)),
        (70, 65, (255, 140, 0, 38)),
    ]:
        glow_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rgba)

    glow = glow.filter(ImageFilter.GaussianBlur(radius=70))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, glow)

    sparkle = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sp_draw = ImageDraw.Draw(sparkle)
    random.seed(42)

    for _ in range(280):
        sx = random.randint(0, w)
        sy = random.randint(0, h)
        dist = math.sqrt((sx - cx) ** 2 + (sy - cy) ** 2)
        max_dist = math.sqrt(w ** 2 + h ** 2) / 2
        prob = max(0, 1 - dist / max_dist) ** 1.3
        if random.random() > prob * 1.2:
            continue
        sz = random.randint(1, 4)
        alpha = random.randint(40, 160)
        sp_draw.ellipse(
            [sx - sz, sy - sz, sx + sz, sy + sz],
            fill=(random.randint(210, 255), random.randint(150, 210),
                  random.randint(0, 50), alpha),
        )

    sparkle = sparkle.filter(ImageFilter.GaussianBlur(radius=1))
    base = Image.alpha_composite(base, sparkle)
    img.paste(base.convert("RGB"))


def _draw_general_background(img):
    draw = ImageDraw.Draw(img)
    w, h = img.size

    for y in range(h):
        t = y / h
        c = _lerp_color((18, 18, 22), (8, 8, 10), t)
        draw.line([(0, y), (w, y)], fill=c)

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    cx, cy = w // 2, int(h * 0.22)

    for rx, ry, rgba in [
        (800, 700, (180, 190, 210, 6)),
        (650, 560, (170, 180, 200, 9)),
        (500, 440, (160, 175, 195, 12)),
        (380, 340, (155, 170, 190, 16)),
        (280, 250, (150, 165, 185, 20)),
        (200, 180, (145, 160, 180, 24)),
        (130, 120, (140, 155, 175, 28)),
        (70, 65, (135, 150, 170, 32)),
    ]:
        glow_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rgba)

    glow = glow.filter(ImageFilter.GaussianBlur(radius=70))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, glow)

    sparkle = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sp_draw = ImageDraw.Draw(sparkle)
    random.seed(99)

    for _ in range(200):
        sx = random.randint(0, w)
        sy = random.randint(0, h)
        dist = math.sqrt((sx - cx) ** 2 + (sy - cy) ** 2)
        max_dist = math.sqrt(w ** 2 + h ** 2) / 2
        prob = max(0, 1 - dist / max_dist) ** 1.3
        if random.random() > prob * 1.0:
            continue
        sz = random.randint(1, 3)
        alpha = random.randint(30, 110)
        sp_draw.ellipse(
            [sx - sz, sy - sz, sx + sz, sy + sz],
            fill=(random.randint(180, 220), random.randint(185, 225),
                  random.randint(200, 240), alpha),
        )

    sparkle = sparkle.filter(ImageFilter.GaussianBlur(radius=1))
    base = Image.alpha_composite(base, sparkle)
    img.paste(base.convert("RGB"))


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _center_x(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    return (WIDTH - tw) // 2


def _draw_centered_text(draw, y, text, font, fill):
    x = _center_x(draw, text, font)
    draw.text((x, y), text, fill=fill, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


# ---------------------------------------------------------------------------
# Main ticket generator
# ---------------------------------------------------------------------------

def generate_ticket_png(*, lead: dict[str, Any], tier: str, event: dict[str, Any]) -> dict[str, str]:
    """Generate a premium ticket image and store a mapping in touchpoints.

    Returns {ticket_id, token, file_path}
    """
    tid = _ticket_id()
    tok = _ticket_token()

    name = _safe_text(lead.get("name") or "Participante")
    email = _safe_text(lead.get("email") or "")
    event_name = _safe_text(event.get("event_name") or "Evento", max_len=40)
    date = _safe_text(event.get("event_date") or "")
    place = _safe_text(event.get("event_place") or "")

    # Stable code for scanning
    raw = f"{lead.get('lead_id')}|{tid}|{tier}|{email}".encode("utf-8")
    code = hashlib.sha256(raw).hexdigest()[:10].upper()

    qr_payload = str({
        "ticket_id": tid,
        "code": code,
        "tier": tier,
        "lead_id": lead.get("lead_id"),
        "email": lead.get("email"),
    })

    is_vip = tier.upper() == "VIP"

    # -- Color palette --
    if is_vip:
        accent = (212, 175, 55)
        tier_color = (212, 175, 55)
        subtitle_color = (200, 180, 130)
        muted = (155, 145, 120)
        label_color = (130, 120, 100)
        separator_rgba = (212, 175, 55, 55)
        badge_fill = (212, 175, 55, 20)
        badge_outline = (212, 175, 55, 70)
    else:
        accent = (180, 190, 210)
        tier_color = (190, 200, 220)
        subtitle_color = (160, 170, 185)
        muted = (130, 135, 145)
        label_color = (110, 115, 125)
        separator_rgba = (180, 190, 210, 50)
        badge_fill = (180, 190, 210, 18)
        badge_outline = (180, 190, 210, 60)

    white = (255, 255, 255)
    light_gray = (185, 185, 190)

    # -- Create canvas & draw background --
    img = Image.new("RGB", (WIDTH, HEIGHT))
    if is_vip:
        _draw_vip_background(img)
    else:
        _draw_general_background(img)

    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # -- Fonts --
    f_title = _font(88, "bold")
    f_subtitle = _font(52, "medium")
    f_tier = _font(96, "bold")
    f_name = _font(62, "bold")
    f_email = _font(36, "light")
    f_info = _font(42, "regular")
    f_label = _font(26, "medium")
    f_code = _font(32, "medium")
    f_footer = _font(28, "light")
    f_brand = _font(24, "medium")
    f_address = _font(32, "light")

    y = 100

    # 1. HEADER
    th = _draw_centered_text(draw, y, "BEYOND WEALTH", f_title, white)
    y += th + 20

    # 2. SUBTITLE
    th = _draw_centered_text(draw, y, "MIAMI 2026", f_subtitle, subtitle_color)
    y += th + 60

    # 3. Separator
    sep_margin = 160
    draw.line([(sep_margin, y), (WIDTH - sep_margin, y)], fill=separator_rgba, width=2)
    y += 55

    # 4. TIER BADGE
    tier_text = tier.upper()
    tier_x = _center_x(draw, tier_text, f_tier)
    tier_bbox = draw.textbbox((tier_x, y), tier_text, font=f_tier)
    pad_x, pad_y = 60, 20
    badge_rect = [
        tier_bbox[0] - pad_x, tier_bbox[1] - pad_y,
        tier_bbox[2] + pad_x, tier_bbox[3] + pad_y,
    ]
    draw.rounded_rectangle(badge_rect, radius=24, fill=badge_fill,
                           outline=badge_outline, width=2)
    draw.text((tier_x, y), tier_text, fill=tier_color, font=f_tier)
    y = badge_rect[3] + 60

    # 5. Separator
    draw.line([(sep_margin, y), (WIDTH - sep_margin, y)], fill=separator_rgba, width=2)
    y += 55

    # 6. INFO SECTION
    draw.text((LEFT_MARGIN, y), "ATTENDEE", fill=label_color, font=f_label)
    y += 38
    draw.text((LEFT_MARGIN, y), name, fill=white, font=f_name)
    y += 85
    if email:
        draw.text((LEFT_MARGIN, y), email, fill=light_gray, font=f_email)
    y += 70

    draw.line([(LEFT_MARGIN, y), (WIDTH - LEFT_MARGIN, y)], fill=separator_rgba, width=1)
    y += 40

    if date:
        draw.text((LEFT_MARGIN, y), "DATE", fill=label_color, font=f_label)
        y += 38
        draw.text((LEFT_MARGIN, y), date, fill=white, font=f_info)
        y += 65

    if place:
        draw.text((LEFT_MARGIN, y), "LOCATION", fill=label_color, font=f_label)
        y += 38
        draw.text((LEFT_MARGIN, y), place, fill=white, font=f_info)
        y += 55

    y += 25

    # 7. Separator
    draw.line([(sep_margin, y), (WIDTH - sep_margin, y)], fill=separator_rgba, width=2)
    y += 45

    # 8. VERIFICATION CODE
    cl_x = _center_x(draw, "VERIFICATION CODE", f_label)
    draw.text((cl_x, y), "VERIFICATION CODE", fill=muted, font=f_label)
    y += 36

    code_x = _center_x(draw, code, f_code)
    draw.text((code_x, y), code, fill=accent, font=f_code)
    y += 60

    # 9. QR CODE with solid white background
    qr_img = _make_qr(qr_payload, box_size=10, border=2)
    qr_size = 360
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)

    container_pad = 30
    container_dim = qr_size + container_pad * 2
    container_x = CENTER_X - container_dim // 2
    container_y = y

    draw.rounded_rectangle(
        [container_x, container_y,
         container_x + container_dim, container_y + container_dim],
        radius=20,
        fill=(255, 255, 255, 255),
    )

    img = Image.alpha_composite(img, overlay)

    qr_paste_x = container_x + container_pad
    qr_paste_y = container_y + container_pad
    img.paste(qr_img, (qr_paste_x, qr_paste_y), qr_img)

    y = container_y + container_dim + 40

    # 10. FOOTER
    footer_draw = ImageDraw.Draw(img)

    footer_text = "Present this ticket at the door  \u2022  beyondwealth.miami"
    ft_x = _center_x(footer_draw, footer_text, f_footer)
    footer_draw.text((ft_x, y), footer_text, fill=(*muted[:3], 150), font=f_footer)
    y += 44

    brand_text = "BEYOND WEALTH EXPERIENCES"
    bx = _center_x(footer_draw, brand_text, f_brand)
    footer_draw.text((bx, y), brand_text, fill=(*accent[:3], 80), font=f_brand)

    # SAVE
    final = img.convert("RGB")
    fp = TICKETS_DIR / f"{tid}.png"
    final.save(str(fp), "PNG", optimize=True)

    # Persist mapping for lookup
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
