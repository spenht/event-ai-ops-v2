"""Public lead capture endpoints for external landing pages.

Provides:
- ``POST /v1/leads/capture`` — public endpoint for landing page forms
- ``GET /v1/forms/{campaign_id}`` — embeddable HTML form (iframe-ready)
- ``GET /v1/campaigns/{campaign_id}/wa-links`` — WhatsApp click-to-chat URLs
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import quote_plus
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("lead_capture")

router = APIRouter(tags=["lead-capture"])


# ── Rate limiter ──────────────────────────────────────────────────

_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 30  # requests
RATE_WINDOW = 60  # seconds


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    window_start = now - RATE_WINDOW
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes, intenta en un minuto")
    _rate_store[ip].append(now)


# ── helpers ────────────────────────────────────────────────────────


def _normalize_phone(raw: str) -> str:
    """Normalize phone input to E.164."""
    s = (raw or "").strip()
    if s.startswith("whatsapp:"):
        s = s[9:]
    s = re.sub(r"[\s()\-.]", "", s)
    if not s.startswith("+"):
        if s.startswith("52") and len(s) >= 12:
            s = "+" + s
        elif len(s) == 10:
            s = "+52" + s
        else:
            s = "+" + s
    return s


def _mx_variants(e164: str) -> list[str]:
    if not e164:
        return [""]
    if e164.startswith("+521"):
        return [e164, "+52" + e164[4:]]
    if e164.startswith("+52") and not e164.startswith("+521"):
        return [e164, "+521" + e164[3:]]
    return [e164]


def _wa_number_from_campaign(campaign: dict) -> str:
    """Extract clean phone number from campaign's twilio_whatsapp_from."""
    raw = (campaign.get("twilio_whatsapp_from") or "").strip()
    if raw.startswith("whatsapp:"):
        raw = raw[9:]
    return raw.lstrip("+")


def _build_wa_url(number: str, text: str) -> str:
    """Build WhatsApp click-to-chat URL."""
    return f"https://wa.me/{number}?text={quote_plus(text)}"


# ── Models ─────────────────────────────────────────────────────────


class CaptureRequest(BaseModel):
    campaign_id: str
    name: str = ""
    email: str = ""
    whatsapp: str = ""
    phone: str = ""
    source: str = "landing_page"
    tier_interest: str = ""


# ── Endpoints ──────────────────────────────────────────────────────


@router.post("/v1/leads/capture")
async def capture_lead(request: Request, body: CaptureRequest):
    """Public endpoint for landing pages to submit leads.

    Creates (or updates) a lead in Supabase and returns useful URLs.
    """
    # Rate limit by IP
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    # Validate
    if not body.campaign_id:
        raise HTTPException(status_code=400, detail="campaign_id es obligatorio")
    if not body.email and not body.whatsapp:
        raise HTTPException(status_code=400, detail="Se requiere email o whatsapp")

    # Fetch campaign
    try:
        r = sb.table("campaigns").select("*").eq("id", body.campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
    except Exception as exc:
        logger.error("capture_campaign_fetch_failed err=%s", str(exc)[:200])
        raise HTTPException(status_code=500, detail="Error al cargar campana")

    if not campaign:
        raise HTTPException(status_code=404, detail="Campana no encontrada")

    # Normalize WhatsApp
    wa = _normalize_phone(body.whatsapp or body.phone) if (body.whatsapp or body.phone) else ""

    # Search for existing lead
    lead = None
    if wa:
        for candidate in _mx_variants(wa):
            try:
                lr = (
                    sb.table("leads")
                    .select("*")
                    .eq("campaign_id", body.campaign_id)
                    .eq("whatsapp", candidate)
                    .limit(1)
                    .execute()
                )
                lead = (lr.data or [None])[0]
                if lead:
                    break
            except Exception:
                pass

    if not lead and body.email:
        try:
            lr = (
                sb.table("leads")
                .select("*")
                .eq("campaign_id", body.campaign_id)
                .eq("email", body.email.strip())
                .limit(1)
                .execute()
            )
            lead = (lr.data or [None])[0]
        except Exception:
            pass

    if lead:
        # Update existing lead
        lead_id = lead["lead_id"]
        updates: dict = {}
        if body.name and not lead.get("name"):
            updates["name"] = body.name.strip()
        if body.email and not lead.get("email"):
            updates["email"] = body.email.strip()
        if wa and not lead.get("whatsapp"):
            updates["whatsapp"] = wa
            updates["phone"] = wa
        if body.tier_interest:
            updates["tier_interest"] = body.tier_interest.strip().upper()
        if body.source and (lead.get("source") or "") == "":
            updates["source"] = body.source
        if updates:
            try:
                sb.table("leads").update(updates).eq("lead_id", lead_id).execute()
            except Exception:
                pass
    else:
        # Create new lead
        lead_id = f"LP-{uuid4().hex[:8]}"
        lead = {
            "lead_id": lead_id,
            "campaign_id": body.campaign_id,
            "name": (body.name or "").strip(),
            "email": (body.email or "").strip(),
            "whatsapp": wa,
            "phone": wa,
            "status": "NEW",
            "source": body.source or "landing_page",
            "tier_interest": (body.tier_interest or "").strip().upper(),
        }
        try:
            sb.table("leads").insert(lead).execute()
        except Exception as exc:
            logger.error("capture_lead_create_failed err=%s", str(exc)[:200])
            raise HTTPException(status_code=500, detail="Error al crear lead")

    # Log touchpoint
    try:
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "campaign_id": body.campaign_id,
            "channel": "web",
            "event_type": "lead_captured",
            "payload": {
                "source": body.source,
                "tier_interest": body.tier_interest,
                "ip": client_ip,
            },
        }).execute()
    except Exception:
        pass

    # Build response URLs
    wa_number = _wa_number_from_campaign(campaign)
    event_name = (campaign.get("event_name") or campaign.get("name") or "el evento").strip()
    lead_name = (body.name or "").strip()

    # WhatsApp ticket URL
    whatsapp_ticket_url = ""
    if wa_number:
        wa_msg = (
            f"Hola! Me registre a {event_name}. "
            f"Mi nombre es {lead_name}, "
            f"correo {(body.email or '').strip()}. "
            f"Quiero generar mi boleto general."
        )
        whatsapp_ticket_url = _build_wa_url(wa_number, wa_msg)

    # Stripe checkout URL (for VIP interest)
    checkout_url = None
    tier = (body.tier_interest or "").strip().upper()
    if tier in ("VIP", "VIP_1", "VIP_2") and campaign.get("stripe_secret_key"):
        try:
            from ..services.stripe_checkout import create_vip_checkout_link

            option = 2 if tier == "VIP_2" else 1
            checkout_url = await create_vip_checkout_link(
                lead_id=lead_id,
                event_id=campaign.get("event_id") or "",
                option=option,
                stripe_secret_key=campaign.get("stripe_secret_key", ""),
                stripe_price_ids=campaign.get("stripe_price_ids") or {},
                stripe_success_url=campaign.get("stripe_success_url", ""),
                stripe_cancel_url=campaign.get("stripe_cancel_url", ""),
                campaign_id=body.campaign_id,
                whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
            )
        except Exception as exc:
            logger.error("capture_checkout_failed lead=%s err=%s", lead_id, str(exc)[:200])

    logger.info(
        "lead_captured campaign=%s lead=%s source=%s name=%s",
        body.campaign_id, lead_id, body.source, lead_name,
    )

    return {
        "ok": True,
        "lead_id": lead_id,
        "checkout_url": checkout_url,
        "whatsapp_ticket_url": whatsapp_ticket_url,
        "whatsapp_vip_url": _build_wa_url(wa_number, f"Hola! Quiero la experiencia VIP de {event_name}.") if wa_number else "",
    }


# ── Embeddable form ───────────────────────────────────────────────


@router.get("/v1/forms/{campaign_id}", response_class=HTMLResponse)
async def embeddable_form(
    campaign_id: str,
    request: Request,
    theme: str = "",
    bg: str = "",
    text: str = "",
    accent: str = "",
    card_bg: str = "",
    input_bg: str = "",
    input_border: str = "",
    radius: str = "",
    btn_text: str = "",
    success_color: str = "",
    vip_color: str = "",
    hide_header: str = "",
    hide_footer: str = "",
    title: str = "",
    subtitle: str = "",
    btn_label: str = "",
):
    """Self-contained HTML form for embedding via iframe on external landing pages.

    By default renders a transparent/minimal form that inherits from the parent
    page.  Pass query-params to customise colours and layout:

    **Preset themes** (``?theme=``):
    - ``dark``  – dark card on dark background (the old default)
    - ``light`` – white card on light background

    **Custom colours** (hex without ``#``):
    - ``bg``           – body background  (default: transparent)
    - ``text``         – body text colour  (default: inherit)
    - ``accent``       – focus ring, separator, subtitle  (default: ``3b82f6``)
    - ``card_bg``      – card background  (default: transparent)
    - ``input_bg``     – input background  (default: transparent)
    - ``input_border`` – input border colour  (default: ``d1d5db``)
    - ``radius``       – border-radius in px  (default: ``10``)
    - ``btn_text``     – submit button text colour  (default: ``ffffff``)
    - ``success_color``– green button gradient start (default: ``22c55e``)
    - ``vip_color``    – VIP button gradient start  (default: ``d4af37``)

    **Layout**:
    - ``hide_header=1``  – hide event name header
    - ``hide_footer=1``  – hide "Powered by" footer
    - ``title``          – override header title
    - ``subtitle``       – override header subtitle
    - ``btn_label``      – override submit button label
    """

    try:
        r = sb.table("campaigns").select(
            "id, event_name, name, stripe_secret_key, stripe_price_ids, twilio_whatsapp_from"
        ).eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
    except Exception:
        campaign = None

    if not campaign:
        return HTMLResponse("<h2>Campana no encontrada</h2>", status_code=404)

    event_name = (campaign.get("event_name") or campaign.get("name") or "Evento").strip()
    has_stripe = bool(campaign.get("stripe_secret_key"))
    wa_number = _wa_number_from_campaign(campaign)

    # ── Resolve theme variables ──────────────────────────────────
    _HEX = re.compile(r"^[0-9a-fA-F]{3,8}$")

    def _hex(val: str, fallback: str) -> str:
        v = val.strip().lstrip("#")
        return v if _HEX.match(v) else fallback

    theme = (theme or "").strip().lower()

    # Aliases: bare "transparent" maps to light variant
    if theme in ("transparent", ""):
        theme = "transparent-light"

    if theme == "dark":
        # Solid dark card on dark background
        v_bg            = _hex(bg,           "0f0f0f")
        v_text          = _hex(text,         "ffffff")
        v_accent        = _hex(accent,       "53c1de")
        v_card_bg       = _hex(card_bg,      "1a1a2e")
        v_card_border   = _hex(input_border, "0f3460")
        v_input_bg      = _hex(input_bg,     "0a0e1a")
        v_input_border  = _hex(input_border, "0f3460")
        v_label_color   = _hex(text,         "8ab4c8")
        v_placeholder   = "3a4a5a"
        v_footer_color  = "3a4a5a"
        v_shadow        = "0 0 60px rgba(15,52,96,0.15)"
    elif theme == "light":
        # Solid white card on light background
        v_bg            = _hex(bg,           "f9fafb")
        v_text          = _hex(text,         "111827")
        v_accent        = _hex(accent,       "3b82f6")
        v_card_bg       = _hex(card_bg,      "ffffff")
        v_card_border   = _hex(input_border, "e5e7eb")
        v_input_bg      = _hex(input_bg,     "f9fafb")
        v_input_border  = _hex(input_border, "d1d5db")
        v_label_color   = _hex(text,         "374151")
        v_placeholder   = "9ca3af"
        v_footer_color  = "9ca3af"
        v_shadow        = "0 4px 24px rgba(0,0,0,0.06)"
    elif theme == "transparent-dark":
        # Transparent — tuned for dark parent backgrounds
        v_bg            = _hex(bg,           "")
        v_text          = _hex(text,         "")
        v_accent        = _hex(accent,       "53c1de")
        v_card_bg       = _hex(card_bg,      "")
        v_card_border   = _hex(input_border, "")
        v_input_bg      = _hex(input_bg,     "")
        v_input_border  = _hex(input_border, "444c56")
        v_label_color   = _hex(text,         "")
        v_placeholder   = "6b7280"
        v_footer_color  = "6b7280"
        v_shadow        = "none"
    else:
        # transparent-light (DEFAULT) — tuned for light parent backgrounds
        v_bg            = _hex(bg,           "")
        v_text          = _hex(text,         "")
        v_accent        = _hex(accent,       "3b82f6")
        v_card_bg       = _hex(card_bg,      "")
        v_card_border   = _hex(input_border, "")
        v_input_bg      = _hex(input_bg,     "")
        v_input_border  = _hex(input_border, "d1d5db")
        v_label_color   = _hex(text,         "")
        v_placeholder   = "9ca3af"
        v_footer_color  = "9ca3af"
        v_shadow        = "none"

    v_radius        = radius.strip() if radius.strip().isdigit() else "10"
    v_btn_text      = _hex(btn_text,       "ffffff")
    v_success       = _hex(success_color,  "22c55e")
    v_vip           = _hex(vip_color,      "d4af37")

    # CSS helpers — only set property if value is non-empty
    def _prop(prop: str, val: str, prefix: str = "#") -> str:
        return f"{prop}: {prefix}{val};" if val else ""

    css_body_bg     = _prop("background", v_bg) if v_bg else "background: transparent;"
    css_body_color  = _prop("color", v_text) if v_text else "color: inherit;"
    css_card_bg     = _prop("background", v_card_bg) if v_card_bg else "background: transparent;"
    css_card_border = f"border: 1px solid #{v_card_border};" if v_card_border else "border: none;"
    css_input_bg    = _prop("background", v_input_bg) if v_input_bg else "background: transparent;"
    css_label_color = _prop("color", v_label_color) if v_label_color else "color: inherit;"

    show_header     = hide_header.strip() != "1"
    show_footer     = hide_footer.strip() != "1"
    form_title      = title.strip() if title.strip() else event_name
    form_subtitle   = subtitle.strip() if subtitle.strip() else "Registro"
    form_btn_label  = btn_label.strip() if btn_label.strip() else "Registrarme"

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Registro — {event_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  {css_body_bg}
  {css_body_color}
  font-family: inherit;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
}}
.card {{
  width: 100%;
  max-width: 440px;
  {css_card_bg}
  {css_card_border}
  border-radius: {v_radius}px;
  padding: 32px 24px;
  box-shadow: {v_shadow};
}}
.header {{
  text-align: center;
  margin-bottom: 24px;
}}
.header h1 {{
  font-size: 22px;
  font-weight: 700;
  color: inherit;
  letter-spacing: 2px;
  margin-bottom: 4px;
}}
.header h2 {{
  font-size: 14px;
  font-weight: 400;
  color: #{v_accent};
}}
.sep {{
  height: 1px;
  background: linear-gradient(90deg, transparent, #{v_accent}66, transparent);
  margin: 18px 0;
}}
label {{
  display: block;
  font-size: 13px;
  {css_label_color}
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 6px;
  margin-top: 14px;
}}
input {{
  width: 100%;
  padding: 13px 16px;
  font-size: 16px;
  border: 1px solid #{v_input_border};
  border-radius: {v_radius}px;
  {css_input_bg}
  color: inherit;
  outline: none;
  transition: border-color 0.2s;
}}
input:focus {{ border-color: #{v_accent}; }}
input::placeholder {{ color: #{v_placeholder}; }}
button {{
  width: 100%;
  margin-top: 20px;
  padding: 15px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 1px;
  border: none;
  border-radius: {v_radius}px;
  cursor: pointer;
  transition: opacity 0.2s, transform 0.1s;
}}
button:hover {{ opacity: 0.9; }}
button:active {{ transform: scale(0.98); }}
.btn-primary {{
  background: #{v_accent};
  color: #{v_btn_text};
}}
.btn-success {{
  background: #{v_success};
  color: #fff;
  text-decoration: none;
  display: inline-block;
  text-align: center;
  padding: 15px;
  border-radius: {v_radius}px;
  font-size: 16px;
  font-weight: 600;
  margin-top: 12px;
  width: 100%;
}}
.btn-vip {{
  background: #{v_vip};
  color: #1a1000;
  text-decoration: none;
  display: inline-block;
  text-align: center;
  padding: 15px;
  border-radius: {v_radius}px;
  font-size: 16px;
  font-weight: 600;
  margin-top: 12px;
  width: 100%;
}}
.msg {{
  margin-top: 20px;
  padding: 16px;
  border-radius: {v_radius}px;
  font-size: 15px;
  line-height: 1.5;
  text-align: center;
}}
.msg-success {{
  background: #{v_success}18;
  border: 1px solid #{v_success}44;
  color: #{v_success};
}}
.msg-error {{
  background: #dc262618;
  border: 1px solid #ef444444;
  color: #ef4444;
}}
#result {{ display: none; }}
.footer {{
  text-align: center;
  margin-top: 20px;
  font-size: 11px;
  color: #{v_footer_color};
}}
</style>
</head>
<body>
<div class="card">
  {"" if not show_header else f'''<div class="header">
    <h1>{form_title}</h1>
    <h2>{form_subtitle}</h2>
  </div>
  <div class="sep"></div>'''}

  <form id="captureForm">
    <label for="name">Nombre completo</label>
    <input type="text" id="name" name="name" placeholder="Tu nombre" required>

    <label for="email">Correo electronico</label>
    <input type="email" id="email" name="email" placeholder="tu@correo.com" required>

    <label for="whatsapp">WhatsApp</label>
    <input type="tel" id="whatsapp" name="whatsapp" placeholder="+521XXXXXXXXXX" inputmode="tel" required>

    <button type="submit" class="btn-primary" id="submitBtn">{form_btn_label}</button>
  </form>

  <div id="result"></div>
  {"" if not show_footer else '<div class="footer">Powered by Event AI Ops</div>'}
</div>

<script>
const CAMPAIGN_ID = "{campaign_id}";
const EVENT_NAME = "{event_name}";
const WA_NUMBER = "{wa_number}";
const HAS_STRIPE = {"true" if has_stripe else "false"};
const API_BASE = window.location.origin;

document.getElementById('captureForm').addEventListener('submit', async function(e) {{
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Registrando...';

  const name = document.getElementById('name').value.trim();
  const email = document.getElementById('email').value.trim();
  const whatsapp = document.getElementById('whatsapp').value.trim();

  try {{
    const res = await fetch(API_BASE + '/v1/leads/capture', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        campaign_id: CAMPAIGN_ID,
        name: name,
        email: email,
        whatsapp: whatsapp,
        source: 'landing_page_form',
        tier_interest: 'GENERAL',
      }}),
    }});
    const data = await res.json();

    if (!res.ok) {{
      throw new Error(data.detail || 'Error al registrar');
    }}

    // Hide form, show result
    document.getElementById('captureForm').style.display = 'none';
    const resultDiv = document.getElementById('result');
    resultDiv.style.display = 'block';

    let html = '<div class="msg msg-success">Registro exitoso!</div>';

    // WhatsApp ticket button
    if (data.whatsapp_ticket_url) {{
      html += '<a href="' + data.whatsapp_ticket_url + '" target="_blank" class="btn-success">';
      html += 'Obtener Boleto General por WhatsApp</a>';
    }}

    // VIP upgrade button
    if (HAS_STRIPE && data.whatsapp_vip_url) {{
      html += '<a href="' + data.whatsapp_vip_url + '" target="_blank" class="btn-vip">';
      html += 'Quiero la Experiencia VIP</a>';
    }}

    resultDiv.innerHTML = html;

  }} catch (err) {{
    btn.disabled = false;
    btn.textContent = '{form_btn_label}';
    const resultDiv = document.getElementById('result');
    resultDiv.style.display = 'block';
    resultDiv.innerHTML = '<div class="msg msg-error">' + (err.message || 'Error') + '</div>';
    setTimeout(() => {{ resultDiv.style.display = 'none'; }}, 4000);
  }}
}});
</script>
</body>
</html>"""


# ── WhatsApp link generator ───────────────────────────────────────


@router.get("/v1/campaigns/{campaign_id}/wa-links")
async def wa_links(campaign_id: str, key: str = ""):
    """Return pre-built WhatsApp click-to-chat URLs for a campaign.

    Auth: spartans_key.
    """
    try:
        r = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
    except Exception:
        campaign = None

    if not campaign:
        raise HTTPException(status_code=404, detail="Campana no encontrada")

    expected_key = (campaign.get("spartans_key") or "").strip()
    if not expected_key or key.strip() != expected_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    wa_number = _wa_number_from_campaign(campaign)
    event_name = (campaign.get("event_name") or campaign.get("name") or "el evento").strip()

    if not wa_number:
        raise HTTPException(status_code=400, detail="La campana no tiene numero de WhatsApp configurado")

    return {
        "ok": True,
        "event_name": event_name,
        "whatsapp_number": wa_number,
        "general_ticket_url": _build_wa_url(
            wa_number,
            f"Hola! Me registre a {event_name}. Quiero generar mi boleto general.",
        ),
        "vip_interest_url": _build_wa_url(
            wa_number,
            f"Hola! Quiero la experiencia VIP de {event_name}.",
        ),
        "general_ticket_url_template": _build_wa_url(
            wa_number,
            f"Hola! Me registre a {event_name}. Mi nombre es [NOMBRE], correo [EMAIL]. Quiero generar mi boleto general.",
        ),
        "vip_purchase_confirmation_url": _build_wa_url(
            wa_number,
            f"Hola! Ya compre mi boleto VIP de {event_name}. Quiero recibir mi boleto por WhatsApp.",
        ),
    }
