from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..deps import sb
from ..settings import settings
from ..services.tickets import generate_ticket_png
from ..services.twilio_whatsapp import send_whatsapp
from ..services.url_shortener import create_short_url
from ..services.google_sheets import sync_lead_to_all_leads_sheet

logger = logging.getLogger("spartans")

router = APIRouter(prefix="/spartans", tags=["spartans"])


# ── helpers ────────────────────────────────────────────────────────


def _event_facts(event_id: str | None) -> dict:
    """Load event data (copy from payments.py — kept independent)."""
    event = {}
    if event_id:
        try:
            ev = sb.table("events").select("*").eq("event_id", event_id).limit(1).execute()
            event = (ev.data or [{}])[0] or {}
        except Exception:
            event = {}

    return {
        "event_id": event_id,
        "event_name": (event.get("event_name") or settings.event_name or "Evento").strip(),
        "event_date": (str(event.get("starts_at") or "") or settings.event_date or "").strip(),
        "event_place": (event.get("address") or settings.event_place or "").strip(),
        "event_speakers": (event.get("speakers") or settings.event_speakers or "").strip(),
    }


def _mx_variants(e164: str) -> list[str]:
    """Return +52 / +521 variants for Mexican numbers (copy from whatsapp.py)."""
    if not e164:
        return [""]
    if e164.startswith("+521"):
        return [e164, "+52" + e164[4:]]
    if e164.startswith("+52") and not e164.startswith("+521"):
        return [e164, "+521" + e164[3:]]
    return [e164]


def _normalize_input(raw: str) -> str:
    """Normalize seller-typed WhatsApp input to E.164."""
    s = (raw or "").strip()
    if s.startswith("whatsapp:"):
        s = s[9:]
    # Remove spaces, dashes, parentheses, dots
    s = re.sub(r"[\s()\-.]", "", s)
    if not s.startswith("+"):
        if s.startswith("52") and len(s) >= 12:
            s = "+" + s
        elif len(s) == 10:
            s = "+52" + s
        else:
            s = "+" + s
    return s


def _html_page(*, message: str = "", error: str = "", wa_value: str = "", key: str = "") -> str:
    """Return complete HTML page for the Spartans VIP confirmation form."""

    msg_html = ""
    if message:
        msg_html = f'<div class="msg success">{message}</div>'
    if error:
        msg_html = f'<div class="msg error">{error}</div>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spartans — Confirmar VIP</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #1a1000;
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
  max-width: 420px;
  background: linear-gradient(135deg, #2a1a00 0%, #1a1000 100%);
  border: 1px solid #d4af3744;
  border-radius: 16px;
  padding: 32px 24px;
  box-shadow: 0 0 60px rgba(212,175,55,0.08);
}}
.header {{
  text-align: center;
  margin-bottom: 28px;
}}
.header h1 {{
  font-size: 28px;
  font-weight: 700;
  color: #fff;
  letter-spacing: 3px;
  margin-bottom: 4px;
}}
.header h2 {{
  font-size: 16px;
  font-weight: 400;
  color: #d4af37;
  letter-spacing: 2px;
}}
.sep {{
  height: 1px;
  background: linear-gradient(90deg, transparent, #d4af3766, transparent);
  margin: 20px 0;
}}
label {{
  display: block;
  font-size: 13px;
  color: #c8b88a;
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 8px;
}}
input[type="tel"] {{
  width: 100%;
  padding: 14px 16px;
  font-size: 18px;
  border: 1px solid #d4af3744;
  border-radius: 10px;
  background: #0d0800;
  color: #fff;
  outline: none;
  transition: border-color 0.2s;
}}
input[type="tel"]:focus {{
  border-color: #d4af37;
}}
input[type="tel"]::placeholder {{
  color: #665a3a;
}}
button {{
  width: 100%;
  margin-top: 20px;
  padding: 16px;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 1px;
  border: none;
  border-radius: 10px;
  background: linear-gradient(135deg, #d4af37, #b8962e);
  color: #1a1000;
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
.tip {{
  background: #d4af3712;
  border: 1px solid #d4af3730;
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
  color: #c8b88a;
  line-height: 1.5;
  margin-bottom: 20px;
}}
.footer {{
  text-align: center;
  margin-top: 24px;
  font-size: 12px;
  color: #665a3a;
}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>BEYOND WEALTH</h1>
    <h2>Confirmacion VIP</h2>
  </div>
  <div class="sep"></div>
  <div class="tip">💡 Si pasaron mas de 24 hrs desde el ultimo mensaje del cliente, pidele que mande un <b>"hola"</b> al WhatsApp antes de confirmar aqui.</div>
  <form method="POST" action="/spartans/confirm-vip?key={key}" id="vipForm">
    <label for="whatsapp">WhatsApp del cliente</label>
    <input type="tel" id="whatsapp" name="whatsapp" placeholder="+521XXXXXXXXXX"
           value="{wa_value}" autocomplete="tel" inputmode="tel" required>
    <button type="submit" id="submitBtn">Confirmar VIP</button>
  </form>
  {msg_html}
  <div class="footer">Event AI Ops — Spartans</div>
</div>
<script>
document.getElementById('vipForm').addEventListener('submit', function() {{
  var btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Procesando...';
}});
</script>
</body>
</html>"""


# ── endpoints ──────────────────────────────────────────────────────


@router.get("/confirm-vip", response_class=HTMLResponse)
async def spartans_form(key: str = ""):
    """Render the VIP confirmation form."""
    if not settings.spartans_key or key != settings.spartans_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return _html_page(key=key)


@router.post("/confirm-vip", response_class=HTMLResponse)
async def spartans_confirm(request: Request, key: str = ""):
    """Process VIP confirmation: update lead, generate ticket, send via WhatsApp."""

    # 1. Auth
    if not settings.spartans_key or key != settings.spartans_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    # 2. Parse form
    form = await request.form()
    raw_wa = str(form.get("whatsapp") or "").strip()
    if not raw_wa:
        return _html_page(error="Ingresa un numero de WhatsApp", key=key)

    # 3. Normalize
    e164 = _normalize_input(raw_wa)

    # 4. Look up lead by WhatsApp (with Mexican variants)
    lead = None
    matched_wa = e164
    for candidate in _mx_variants(e164):
        try:
            lr = sb.table("leads").select("*").eq("whatsapp", candidate).limit(1).execute()
            lead = (lr.data or [None])[0]
            if lead:
                matched_wa = candidate
                break
        except Exception:
            pass

    if not lead:
        return _html_page(
            error=f"No se encontro un lead con WhatsApp {e164}",
            wa_value=raw_wa,
            key=key,
        )

    lead_id = lead.get("lead_id", "")
    lead_name = (lead.get("name") or "").strip() or "Sin nombre"

    # 5. Already VIP?
    current_status = (lead.get("status") or "").strip().upper()
    if current_status == "VIP_PAID":
        return _html_page(
            message=f"✅ {lead_name} ya es VIP (ya estaba confirmado)",
            wa_value="",
            key=key,
        )

    # 6. Mark as VIP_PAID + PAID
    try:
        sb.table("leads").update({
            "payment_status": "PAID",
            "status": "VIP_PAID",
        }).eq("lead_id", lead_id).execute()
        logger.info("spartans_vip_confirmed lead=%s name=%s", lead_id, lead_name)
    except Exception as exc:
        logger.error("spartans_update_failed lead=%s err=%s", lead_id, str(exc)[:300])
        return _html_page(
            error=f"Error al actualizar lead: {str(exc)[:100]}",
            wa_value=raw_wa,
            key=key,
        )

    # Re-fetch updated lead
    try:
        lr2 = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
        lead = (lr2.data or [lead])[0] or lead
    except Exception:
        pass

    # 7. Sync to Google Sheets
    try:
        asyncio.create_task(sync_lead_to_all_leads_sheet(lead))
    except Exception:
        pass

    # 8. Event facts
    event_id = (lead.get("event_id") or settings.default_event_id or "").strip() or None
    facts = _event_facts(event_id)

    # 9. Generate VIP ticket
    ticket = generate_ticket_png(lead=lead, tier="VIP", event=facts)

    # 10. Send ticket + follow-up messages via WhatsApp
    wa = matched_wa

    if not settings.public_base_url:
        msg = (
            "✅ Pago recibido. Ya quedaste como VIP.\n\n"
            "(Nota: falta PUBLIC_BASE_URL para mandar el QR automatico.)"
        )
        try:
            await send_whatsapp(wa, msg)
        except Exception:
            pass
    else:
        media = f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
        msg = (
            "✅ ¡Listo! Pago confirmado.\n"
            "Aqui esta tu boleto VIP con tu QR (guardalo).\n\n"
            "Te voy a compartir un video con algunos testimonios para que veas la transformacion que te espera en Beyond Wealth."
        )
        try:
            await send_whatsapp(wa, msg, media_urls=[media])
        except Exception as e:
            logger.error("spartans_send_ticket_failed %s", str(e)[:300])

        # Log ticket_sent
        try:
            sb.table("touchpoints").insert({
                "lead_id": lead_id,
                "channel": "whatsapp",
                "event_type": "ticket_sent",
                "payload": {"tier": "VIP", "ticket_id": ticket["ticket_id"], "source": "spartans_manual"},
            }).execute()
        except Exception:
            pass

        # 5s delay so ticket arrives before video
        await asyncio.sleep(5)

        # Testimonials video
        testimonial_url = (settings.whatsapp_video_testimonios or "").strip() if hasattr(settings, "whatsapp_video_testimonios") else ""
        if testimonial_url and testimonial_url.startswith("https://"):
            try:
                await send_whatsapp(
                    wa,
                    "🎬 Te comparto un video con algunos testimonios para que veas la transformacion que te espera en Beyond Wealth 👇",
                    media_urls=[testimonial_url],
                )
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "whatsapp",
                    "event_type": "media_sent",
                    "payload": {"key": "testimonios", "url": testimonial_url},
                }).execute()
            except Exception:
                pass

            # Closing message
            try:
                event_name = facts.get("event_name") or "Beyond Wealth"
                closing = (
                    f"Estoy muy emocionada de que vayas a ser parte del grupo VIP de *{event_name}*, "
                    "un evento que va a marcar un antes y un despues en tu vida.\n\n"
                    "Cualquier pregunta que tengas, aqui estoy para servirte."
                ).strip()
                await send_whatsapp(wa, closing)
            except Exception:
                pass

            # Log outbound_ai summary
            try:
                webhook_summary = (
                    msg + "\n\n"
                    "🎬 Te comparto un video con algunos testimonios para que veas la transformacion que te espera en Beyond Wealth 👇\n\n"
                    + closing
                )
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "whatsapp",
                    "event_type": "outbound_ai",
                    "payload": {"to": f"whatsapp:{wa}", "body": webhook_summary, "source": "spartans_manual"},
                }).execute()
            except Exception:
                pass

            # Schedule calendar reminder (~10 min later)
            try:
                e_name = facts.get("event_name") or "Beyond Wealth"
                e_place = facts.get("event_place") or ""
                e_speakers = facts.get("event_speakers") or ""
                details = f"{e_name}\nSpeakers: {e_speakers}\nLugar: {e_place}"
                cal_url = (
                    "https://calendar.google.com/calendar/render?"
                    f"action=TEMPLATE"
                    f"&text={quote_plus(e_name)}"
                    f"&details={quote_plus(details)}"
                    f"&dates=20260327T150000Z/20260330T013000Z"
                    f"&location={quote_plus(e_place)}"
                )
                cal_url = await create_short_url(cal_url, lead_id=lead_id, url_type="calendar", prefix="cal_")
                send_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                cal_msg = (
                    f"{lead_name} 😊 quise tomarme la libertad de mandarte nuevamente la liga "
                    "para que agregues el evento a tu calendario y lo tengas super presente, "
                    "ahi viene la direccion del lugar tambien, de esa manera tienes todo a la mano "
                    "ya en tu agenda. Solo dale click abajo y dale aceptar y listo :)\n\n"
                    f"📅 {cal_url}"
                ).strip()
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "whatsapp",
                    "event_type": "scheduled_message",
                    "payload": {
                        "type": "calendar_reminder",
                        "send_after": send_at,
                        "status": "pending",
                        "body": cal_msg,
                        "wa": wa,
                    },
                }).execute()
            except Exception:
                pass

    # 11. Return success
    return _html_page(
        message=f"✅ VIP confirmado para {lead_name} — boleto enviado por WhatsApp",
        wa_value="",
        key=key,
    )
