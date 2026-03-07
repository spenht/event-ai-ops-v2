#!/usr/bin/env python3
"""
PREMIUM Ticket Design Generator — v4
=====================================
Changes from v3:
  - Smartphone 9:16 ratio (1080x1920) for optimal mobile viewing
  - Bigger QR code (360px) with solid white rounded background for contrast
  - GENERAL: Beyond Wealth brand colors (dark + silver/platinum accents)
  - Date shows all 3 days: "27 - 29 de Marzo, 2026"
  - More vertical breathing room with taller canvas

Output:
    /tmp/tickets/sample_VIP.png
    /tmp/tickets/sample_GENERAL.png
"""

import math
import os
import random
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 1080, 1920  # 9:16 smartphone ratio
LEFT_MARGIN = 90
CENTER_X = WIDTH // 2
OUTPUT_DIR = Path("/tmp/tickets")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = "/System/Library/Fonts/HelveticaNeue.ttc"
# Font indices: 0=Regular, 1=Bold, 4=Condensed Bold, 5=UltraLight,
#               7=Light, 10=Medium, 12=Thin

SAMPLE_DATA = {
    "name": "Florencia Montoya",
    "email": "florencia@example.com",
    "date": "27 - 29 de Marzo, 2026",
    "place": "Miami, FL",
    "address": "Hilton Miami Downtown, 1601 Biscayne Blvd",
    "code": "BW-2026-FLM-4829",
}


def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Load Helvetica Neue at desired weight with fallbacks."""
    weight_map = {
        "bold": 1, "condensed_bold": 4, "medium": 10,
        "regular": 0, "light": 7, "thin": 12, "ultralight": 5,
    }
    idx = weight_map.get(weight, 0)
    try:
        return ImageFont.truetype(FONT_PATH, size, index=idx)
    except Exception:
        for fb in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            try:
                return ImageFont.truetype(fb, size)
            except Exception:
                continue
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Color utilities
# ---------------------------------------------------------------------------

def lerp_color(c1, c2, t):
    """Linearly interpolate between two RGB/RGBA tuples."""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(len(c1)))


def multi_stop_gradient_value(t, stops):
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            local_t = (t - t0) / (t1 - t0) if t1 != t0 else 0
            return lerp_color(c0, c1, local_t)
    return stops[-1][1]


# ---------------------------------------------------------------------------
# Background renderers
# ---------------------------------------------------------------------------

def draw_vip_background(img):
    """
    VIP background: dark warm brown-black base with radial golden bloom
    in the upper-center, plus golden sparkle particles.
    """
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Vertical gradient base: dark warm brown-black to near-black
    for y in range(h):
        t = y / h
        c = lerp_color((26, 16, 0), (10, 8, 5), t)
        draw.line([(0, y), (w, y)], fill=c)

    # Golden radial glow — layered ellipses on RGBA overlay
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
        (70,  65,  (255, 140, 0, 38)),
    ]:
        glow_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rgba)

    glow = glow.filter(ImageFilter.GaussianBlur(radius=70))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, glow)

    # Sparkle / particle effects
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


def draw_general_background(img):
    """
    GENERAL background: Dark premium base with subtle silver/cool glow.
    Keeps Beyond Wealth dark brand feel but differentiates from VIP
    with cooler silver-platinum tones instead of gold.
    """
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Vertical gradient base: dark charcoal with slight warm undertone
    for y in range(h):
        t = y / h
        c = lerp_color((18, 18, 22), (8, 8, 10), t)
        draw.line([(0, y), (w, y)], fill=c)

    # Silver/platinum radial glow
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
        (70,  65,  (135, 150, 170, 32)),
    ]:
        glow_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rgba)

    glow = glow.filter(ImageFilter.GaussianBlur(radius=70))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, glow)

    # Subtle sparkle particles (silver/white)
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
# QR Code generation
# ---------------------------------------------------------------------------

def make_qr(data_str, box_size=10, border=2):
    """Generate a black-on-white QR code image."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data_str)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    return qr_img.convert("RGBA")


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def center_x(draw, text, font):
    """Compute x position to horizontally center text on the canvas."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    return (WIDTH - tw) // 2


def draw_centered_text(draw, y, text, font, fill):
    """Draw text centered. Returns text height."""
    x = center_x(draw, text, font)
    draw.text((x, y), text, fill=fill, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


# ---------------------------------------------------------------------------
# Main ticket renderer
# ---------------------------------------------------------------------------

def generate_ticket(tier: str, data: dict, output_path: Path):
    """Render and save a single ticket image."""
    is_vip = tier.upper() == "VIP"

    # -- Color palette --
    if is_vip:
        accent = (212, 175, 55)          # Gold
        accent_bright = (228, 199, 100)
        tier_color = (212, 175, 55)
        subtitle_color = (200, 180, 130)
        muted = (155, 145, 120)
        label_color = (130, 120, 100)
        separator_rgba = (212, 175, 55, 55)
        badge_fill = (212, 175, 55, 20)
        badge_outline = (212, 175, 55, 70)
    else:
        accent = (180, 190, 210)         # Silver/Platinum
        accent_bright = (200, 210, 225)
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
        draw_vip_background(img)
    else:
        draw_general_background(img)

    # Convert to RGBA for compositing transparent elements
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # -- Load fonts --
    f_title     = _font(88, "bold")     # "BEYOND WEALTH"
    f_subtitle  = _font(52, "medium")   # "MIAMI 2026"
    f_tier      = _font(96, "bold")     # "VIP" / "GENERAL"
    f_name      = _font(62, "bold")     # Attendee name
    f_email     = _font(36, "light")    # Email
    f_info      = _font(42, "regular")  # Date, Place
    f_label     = _font(26, "medium")   # Section labels
    f_code      = _font(32, "medium")   # Verification code
    f_footer    = _font(28, "light")    # Footer text
    f_brand     = _font(24, "medium")   # Brand line

    y = 100  # Start cursor (more top padding for taller canvas)

    # ===================================================================
    # 1. HEADER — "BEYOND WEALTH" centered
    # ===================================================================
    th = draw_centered_text(draw, y, "BEYOND WEALTH", f_title, white)
    y += th + 20

    # ===================================================================
    # 2. "MIAMI 2026" centered
    # ===================================================================
    th = draw_centered_text(draw, y, "MIAMI 2026", f_subtitle, subtitle_color)
    y += th + 60

    # ===================================================================
    # 3. Separator line
    # ===================================================================
    sep_margin = 160
    draw.line(
        [(sep_margin, y), (WIDTH - sep_margin, y)],
        fill=separator_rgba, width=2,
    )
    y += 55

    # ===================================================================
    # 4. TIER BADGE — centered, with rounded outline container
    # ===================================================================
    tier_text = tier.upper()
    tier_x = center_x(draw, tier_text, f_tier)
    tier_bbox = draw.textbbox((tier_x, y), tier_text, font=f_tier)
    pad_x, pad_y = 60, 20
    badge_rect = [
        tier_bbox[0] - pad_x,
        tier_bbox[1] - pad_y,
        tier_bbox[2] + pad_x,
        tier_bbox[3] + pad_y,
    ]
    draw.rounded_rectangle(badge_rect, radius=24, fill=badge_fill,
                           outline=badge_outline, width=2)
    draw.text((tier_x, y), tier_text, fill=tier_color, font=f_tier)
    y = badge_rect[3] + 60

    # ===================================================================
    # 5. Separator line
    # ===================================================================
    draw.line(
        [(sep_margin, y), (WIDTH - sep_margin, y)],
        fill=separator_rgba, width=2,
    )
    y += 55

    # ===================================================================
    # 6. LEFT-ALIGNED INFO SECTION
    # ===================================================================

    # -- ATTENDEE label --
    draw.text((LEFT_MARGIN, y), "ATTENDEE", fill=label_color, font=f_label)
    y += 38

    # -- Name (big, bold, white) --
    draw.text((LEFT_MARGIN, y), data["name"], fill=white, font=f_name)
    y += 85

    # -- Email (smaller, muted) --
    draw.text((LEFT_MARGIN, y), data["email"], fill=light_gray, font=f_email)
    y += 70

    # -- Thin separator --
    draw.line(
        [(LEFT_MARGIN, y), (WIDTH - LEFT_MARGIN, y)],
        fill=separator_rgba, width=1,
    )
    y += 40

    # -- DATE label --
    draw.text((LEFT_MARGIN, y), "DATE", fill=label_color, font=f_label)
    y += 38

    # -- Date value --
    draw.text((LEFT_MARGIN, y), data["date"], fill=white, font=f_info)
    y += 65

    # -- LOCATION label --
    draw.text((LEFT_MARGIN, y), "LOCATION", fill=label_color, font=f_label)
    y += 38

    # -- Place value --
    draw.text((LEFT_MARGIN, y), data["place"], fill=white, font=f_info)
    y += 55

    # -- Address (smaller, below place) --
    f_address = _font(32, "light")
    address = (data.get("address") or "").strip()
    if address:
        draw.text((LEFT_MARGIN, y), address, fill=light_gray, font=f_address)
        y += 55
    else:
        y += 25

    # ===================================================================
    # 7. Separator line
    # ===================================================================
    draw.line(
        [(sep_margin, y), (WIDTH - sep_margin, y)],
        fill=separator_rgba, width=2,
    )
    y += 45

    # ===================================================================
    # 8. VERIFICATION CODE — centered
    # ===================================================================
    code_label = "VERIFICATION CODE"
    cl_x = center_x(draw, code_label, f_label)
    draw.text((cl_x, y), code_label, fill=muted, font=f_label)
    y += 36

    code_text = data["code"]
    code_x = center_x(draw, code_text, f_code)
    draw.text((code_x, y), code_text, fill=accent, font=f_code)
    y += 60

    # ===================================================================
    # 9. QR CODE — bigger, with solid white rounded background
    # ===================================================================
    qr_data = f"https://beyondwealth.miami/verify/{data['code']}"
    qr_img = make_qr(qr_data, box_size=10, border=2)

    qr_size = 360  # Bigger QR
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)

    container_pad = 30
    container_dim = qr_size + container_pad * 2
    container_x = CENTER_X - container_dim // 2
    container_y = y

    # Solid white rounded background for QR contrast
    draw.rounded_rectangle(
        [container_x, container_y,
         container_x + container_dim, container_y + container_dim],
        radius=20,
        fill=(255, 255, 255, 255),  # Solid white
    )

    # Composite overlay (text + shapes) onto base
    img = Image.alpha_composite(img, overlay)

    # Paste QR code (black on white) with alpha mask
    qr_paste_x = container_x + container_pad
    qr_paste_y = container_y + container_pad
    img.paste(qr_img, (qr_paste_x, qr_paste_y), qr_img)

    y = container_y + container_dim + 40

    # ===================================================================
    # 10. FOOTER
    # ===================================================================
    footer_draw = ImageDraw.Draw(img)

    footer_text = "Present this ticket at the door  •  beyondwealth.miami"
    ft_x = center_x(footer_draw, footer_text, f_footer)
    footer_draw.text((ft_x, y), footer_text,
                     fill=(*muted[:3], 150), font=f_footer)
    y += 44

    brand_text = "BEYOND WEALTH EXPERIENCES"
    bx = center_x(footer_draw, brand_text, f_brand)
    footer_draw.text((bx, y), brand_text,
                     fill=(*accent[:3], 80), font=f_brand)

    # ===================================================================
    # SAVE
    # ===================================================================
    final = img.convert("RGB")
    final.save(str(output_path), "PNG", optimize=True)
    print(f"  -> Saved: {output_path}  ({WIDTH}x{HEIGHT})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Beyond Wealth Miami 2026 — Ticket Design V4")
    print("=" * 50)

    generate_ticket(
        tier="VIP",
        data=SAMPLE_DATA,
        output_path=OUTPUT_DIR / "sample_VIP.png",
    )
    generate_ticket(
        tier="GENERAL",
        data=SAMPLE_DATA,
        output_path=OUTPUT_DIR / "sample_GENERAL.png",
    )

    print("\nDone! Tickets saved to /tmp/tickets/")
