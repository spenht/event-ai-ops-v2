"""Per-campaign ticket issuance form for agents.

Agents open the URL in their browser, enter prospect info (name, email,
WhatsApp) and select a ticket tier.  On submit the system upserts the lead,
generates a branded ticket PNG, and sends it via WhatsApp automatically.

Auth: campaign's ``spartans_key`` passed as ``?key=…`` query parameter.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..deps import sb
from ..settings import settings
from ..services.tickets import generate_ticket_png
from ..services.twilio_whatsapp import send_whatsapp

logger = logging.getLogger("ticket_issue")

router = APIRouter(prefix="/v1/tickets", tags=["ticket-issue"])


# ── helpers ────────────────────────────────────────────────────────


def _normalize_input(raw: str) -> str:
    """Normalize seller-typed WhatsApp input to E.164."""
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
    """Return +52 / +521 variants for Mexican numbers."""
    if not e164:
        return [""]
    if e164.startswith("+521"):
        return [e164, "+52" + e164[4:]]
    if e164.startswith("+52") and not e164.startswith("+521"):
        return [e164, "+521" + e164[3:]]
    return [e164]


def _get_tier_options(stripe_price_ids: dict | None) -> list[dict[str, str]]:
    """Build tier options for the form select dropdown.

    Always includes GENERAL.  Adds paid tiers from ``stripe_price_ids``.
    """
    options: list[dict[str, str]] = [
        {"value": "GENERAL", "label": "General (Gratis)"},
    ]
    if not stripe_price_ids or not isinstance(stripe_price_ids, dict):
        options.append({"value": "VIP_1", "label": "VIP"})
        return options

    for key in sorted(stripe_price_ids.keys()):
        if key == "default":
            continue
        val = stripe_price_ids[key]
        if isinstance(val, dict):
            label = val.get("label", f"VIP Opcion {key}")
            price = val.get("display_price", "")
            display = f"{label} — {price}" if price else label
        else:
            display = f"VIP Opcion {key}"
        options.append({"value": f"VIP_{key}", "label": display})

    return options


def _event_facts_from_campaign(campaign: dict) -> dict:
    """Build event-facts dict that ``generate_ticket_png`` expects."""
    return {
        "event_id": campaign.get("event_id") or campaign.get("id") or "",
        "event_name": (campaign.get("event_name") or "Evento").strip(),
        "event_date": (str(campaign.get("event_date") or "")).strip(),
        "event_place": (campaign.get("event_location") or campaign.get("event_place") or "").strip(),
        "event_speakers": (campaign.get("event_speakers") or "").strip(),
    }


def _twilio_kwargs(campaign: dict) -> dict[str, str]:
    """Extract Twilio creds from campaign for explicit kwarg passing."""
    return {
        "account_sid": (campaign.get("twilio_account_sid") or "").strip(),
        "auth_token": (campaign.get("twilio_auth_token") or "").strip(),
        "whatsapp_from": (campaign.get("twilio_whatsapp_from") or "").strip(),
    }


def _public_base(campaign: dict) -> str:
    return (
        (campaign.get("public_base_url") or "").strip()
        or (settings.public_base_url if hasattr(settings, "public_base_url") else "")
        or ""
    ).rstrip("/")


# ── HTML builder ──────────────────────────────────────────────────


def _html_page(
    *,
    campaign_id: str = "",
    key: str = "",
    event_name: str = "Evento",
    tier_options: list[dict[str, str]] | None = None,
    message: str = "",
    error: str = "",
    wa_value: str = "",
    name_value: str = "",
    email_value: str = "",
) -> str:
    msg_html = ""
    if message:
        msg_html = f'<div class="msg success">{message}</div>'
    if error:
        msg_html = f'<div class="msg error">{error}</div>'

    options_html = ""
    for opt in (tier_options or [{"value": "GENERAL", "label": "General (Gratis)"}]):
        options_html += f'<option value="{opt["value"]}">{opt["label"]}</option>\n'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{event_name} — Emision de Boletos</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #0f0f0f;
  color: #fff;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
}}
.card {{
  width: 100%;
  max-width: 460px;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  border: 1px solid #0f3460aa;
  border-radius: 16px;
  padding: 32px 24px;
  box-shadow: 0 0 60px rgba(15,52,96,0.15);
}}
.header {{
  text-align: center;
  margin-bottom: 28px;
}}
.header h1 {{
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  letter-spacing: 2px;
  margin-bottom: 4px;
}}
.header h2 {{
  font-size: 15px;
  font-weight: 400;
  color: #53c1de;
  letter-spacing: 1px;
}}
.sep {{
  height: 1px;
  background: linear-gradient(90deg, transparent, #53c1de66, transparent);
  margin: 20px 0;
}}
label {{
  display: block;
  font-size: 13px;
  color: #8ab4c8;
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 6px;
  margin-top: 16px;
}}
input, select {{
  width: 100%;
  padding: 13px 16px;
  font-size: 16px;
  border: 1px solid #0f346066;
  border-radius: 10px;
  background: #0a0e1a;
  color: #fff;
  outline: none;
  transition: border-color 0.2s;
}}
input:focus, select:focus {{
  border-color: #53c1de;
}}
input::placeholder {{
  color: #3a4a5a;
}}
select {{
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%2353c1de' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 16px center;
  padding-right: 40px;
}}
button {{
  width: 100%;
  margin-top: 24px;
  padding: 16px;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 1px;
  border: none;
  border-radius: 10px;
  background: linear-gradient(135deg, #53c1de, #0f3460);
  color: #fff;
  cursor: pointer;
  transition: opacity 0.2s, transform 0.1s;
}}
button:hover {{ opacity: 0.9; }}
button:active {{ transform: scale(0.98); }}
button:disabled {{
  opacity: 0.5;
  cursor: not-allowed;
}}
.msg {{
  margin-top: 20px;
  padding: 14px 16px;
  border-radius: 10px;
  font-size: 15px;
  line-height: 1.4;
}}
.success {{
  background: #16a34a22;
  border: 1px solid #22c55e44;
  color: #4ade80;
}}
.error {{
  background: #dc262622;
  border: 1px solid #ef444444;
  color: #f87171;
}}
.footer {{
  text-align: center;
  margin-top: 24px;
  font-size: 12px;
  color: #3a4a5a;
}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>{event_name}</h1>
    <h2>Emision de Boletos</h2>
  </div>
  <div class="sep"></div>
  <form method="POST" action="/v1/tickets/issue/{campaign_id}?key={key}" id="issueForm">
    <label for="name">Nombre completo</label>
    <input type="text" id="name" name="name" placeholder="Juan Perez"
           value="{name_value}" autocomplete="name" required>

    <label for="email">Correo electronico</label>
    <input type="email" id="email" name="email" placeholder="juan@ejemplo.com"
           value="{email_value}" autocomplete="email" required>

    <label for="whatsapp">WhatsApp</label>
    <input type="tel" id="whatsapp" name="whatsapp" placeholder="+521XXXXXXXXXX"
           value="{wa_value}" autocomplete="tel" inputmode="tel" required>

    <label for="tier">Tipo de boleto</label>
    <select id="tier" name="tier">
      {options_html}
    </select>

    <button type="submit" id="submitBtn">Generar y Enviar Boleto</button>
  </form>
  {msg_html}
  <div class="footer">Event AI Ops — Ticket Issuance</div>
</div>
<script>
document.getElementById('issueForm').addEventListener('submit', function() {{
  var btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Generando boleto...';
}});
</script>
</body>
</html>"""


# ── endpoints ──────────────────────────────────────────────────────


def _fetch_campaign(campaign_id: str) -> dict:
    """Fetch campaign row from Supabase."""
    try:
        r = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [None])[0]
        if not campaign:
            raise HTTPException(status_code=404, detail="Campana no encontrada")
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("fetch_campaign_failed id=%s err=%s", campaign_id, str(exc)[:200])
        raise HTTPException(status_code=500, detail="Error al cargar campana")


def _validate_key(campaign: dict, key: str) -> None:
    """Validate spartans_key."""
    expected = (campaign.get("spartans_key") or "").strip()
    if not expected or key.strip() != expected:
        raise HTTPException(status_code=403, detail="Acceso denegado")


@router.get("/issue/{campaign_id}", response_class=HTMLResponse)
async def ticket_issue_form(campaign_id: str, key: str = ""):
    """Render the ticket issuance form."""
    campaign = _fetch_campaign(campaign_id)
    _validate_key(campaign, key)

    event_name = (campaign.get("event_name") or campaign.get("name") or "Evento").strip()
    tier_options = _get_tier_options(campaign.get("stripe_price_ids"))

    return _html_page(
        campaign_id=campaign_id,
        key=key,
        event_name=event_name,
        tier_options=tier_options,
    )


@router.post("/issue/{campaign_id}", response_class=HTMLResponse)
async def ticket_issue_process(campaign_id: str, request: Request, key: str = ""):
    """Process ticket issuance: upsert lead, generate ticket, send via WhatsApp."""

    campaign = _fetch_campaign(campaign_id)
    _validate_key(campaign, key)

    event_name = (campaign.get("event_name") or campaign.get("name") or "Evento").strip()
    tier_options = _get_tier_options(campaign.get("stripe_price_ids"))

    # 1. Parse form
    form = await request.form()
    raw_name = str(form.get("name") or "").strip()
    raw_email = str(form.get("email") or "").strip()
    raw_wa = str(form.get("whatsapp") or "").strip()
    tier_value = str(form.get("tier") or "GENERAL").strip()

    if not raw_name or not raw_wa:
        return _html_page(
            campaign_id=campaign_id, key=key, event_name=event_name,
            tier_options=tier_options,
            error="Nombre y WhatsApp son obligatorios",
            name_value=raw_name, email_value=raw_email, wa_value=raw_wa,
        )

    # 2. Normalize WhatsApp
    e164 = _normalize_input(raw_wa)

    # 3. Determine tier label
    tier_display = "GENERAL"
    if tier_value.startswith("VIP"):
        tier_display = "VIP"

    # 4. Upsert lead
    lead = None
    matched_wa = e164
    for candidate in _mx_variants(e164):
        try:
            lr = (
                sb.table("leads")
                .select("*")
                .eq("campaign_id", campaign_id)
                .eq("whatsapp", candidate)
                .limit(1)
                .execute()
            )
            lead = (lr.data or [None])[0]
            if lead:
                matched_wa = candidate
                break
        except Exception:
            pass

    # 4b. Also search by email (prevents duplicate insert when lead exists with different phone)
    if not lead and raw_email:
        try:
            lr_email = (
                sb.table("leads")
                .select("*")
                .eq("campaign_id", campaign_id)
                .eq("email", raw_email.strip())
                .limit(1)
                .execute()
            )
            lead = (lr_email.data or [None])[0]
        except Exception:
            pass

    new_status = "VIP_PAID" if tier_display == "VIP" else "GENERAL_CONFIRMED"

    if lead:
        # Update existing
        lead_id = lead["lead_id"]
        try:
            sb.table("leads").update({
                "name": raw_name,
                "email": raw_email,
                "status": new_status,
                "payment_status": "PAID" if tier_display == "VIP" else "",
            }).eq("lead_id", lead_id).execute()
        except Exception as exc:
            logger.error("lead_update_failed id=%s err=%s", lead_id, str(exc)[:200])
    else:
        # Create new
        lead_id = f"TI-{secrets.token_hex(4)}"
        lead = {
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "name": raw_name,
            "email": raw_email,
            "whatsapp": e164,
            "phone": e164,
            "status": new_status,
            "payment_status": "PAID" if tier_display == "VIP" else "",
            "source": "agent_ticket_issue",
            "tier_interest": tier_display,
        }
        try:
            sb.table("leads").insert(lead).execute()
        except Exception as exc:
            logger.error("lead_create_failed err=%s", str(exc)[:200])
            return _html_page(
                campaign_id=campaign_id, key=key, event_name=event_name,
                tier_options=tier_options,
                error=f"Error al crear lead: {str(exc)[:100]}",
                name_value=raw_name, email_value=raw_email, wa_value=raw_wa,
            )

    # Re-fetch lead to get latest
    try:
        lr2 = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
        lead = (lr2.data or [lead])[0] or lead
    except Exception:
        pass

    # 5. Generate ticket
    facts = _event_facts_from_campaign(campaign)
    ticket_config = campaign.get("ticket_config") if isinstance(campaign.get("ticket_config"), dict) else None

    try:
        ticket = generate_ticket_png(
            lead=lead,
            tier=tier_display,
            event=facts,
            ticket_config=ticket_config,
            campaign_id=campaign_id,
        )
    except Exception as exc:
        logger.error("ticket_gen_failed lead=%s err=%s", lead_id, str(exc)[:200])
        return _html_page(
            campaign_id=campaign_id, key=key, event_name=event_name,
            tier_options=tier_options,
            error=f"Error al generar boleto: {str(exc)[:100]}",
            name_value=raw_name, email_value=raw_email, wa_value=raw_wa,
        )

    # 6. Send via WhatsApp
    base_url = _public_base(campaign)
    twilio = _twilio_kwargs(campaign)

    if not base_url:
        return _html_page(
            campaign_id=campaign_id, key=key, event_name=event_name,
            tier_options=tier_options,
            error="Falta PUBLIC_BASE_URL en la campana — no se puede enviar el boleto",
            name_value=raw_name, email_value=raw_email, wa_value=raw_wa,
        )

    media_url = f"{base_url}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
    msg = (
        f"Hola {raw_name}! Aqui esta tu boleto {tier_display} para {event_name} "
        f"con tu codigo QR. Guardalo y presentalo en la entrada."
    )

    try:
        await send_whatsapp(matched_wa, msg, media_urls=[media_url], **twilio)
    except Exception as exc:
        logger.error("wa_send_failed lead=%s err=%s", lead_id, str(exc)[:200])
        return _html_page(
            campaign_id=campaign_id, key=key, event_name=event_name,
            tier_options=tier_options,
            error=f"Boleto generado pero fallo el envio por WhatsApp: {str(exc)[:100]}",
            name_value="", email_value="", wa_value="",
        )

    # 7. Log touchpoint
    try:
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "channel": "whatsapp",
            "event_type": "ticket_sent",
            "payload": {
                "tier": tier_display,
                "ticket_id": ticket["ticket_id"],
                "source": "agent_ticket_issue",
            },
        }).execute()
    except Exception:
        pass

    logger.info(
        "ticket_issued campaign=%s lead=%s tier=%s name=%s",
        campaign_id, lead_id, tier_display, raw_name,
    )

    return _html_page(
        campaign_id=campaign_id, key=key, event_name=event_name,
        tier_options=tier_options,
        message=f"Boleto {tier_display} enviado a {raw_name} ({matched_wa}) por WhatsApp",
        name_value="", email_value="", wa_value="",
    )
