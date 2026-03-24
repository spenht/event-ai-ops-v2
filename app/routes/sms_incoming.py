"""
SMS Incoming Webhook — handles replies to outbound SMS campaigns.
When someone replies "SI" to our invitation SMS, we:
1. Generate and send them a ticket (WhatsApp template or SMS)
2. Enqueue them for spartan VIP upsell calls
"""
import logging
from fastapi import APIRouter, Request, Response
from ..settings import settings

logger = logging.getLogger("sms_incoming")

router = APIRouter(prefix="/v1/sms", tags=["sms"])

# Campaign to use for ticket generation (Beyond Wealth Miami)
DEFAULT_CAMPAIGN_ID = "e4809b3b-2fb5-4cfb-957b-d18c16f7942c"

# Positive responses (case-insensitive)
POSITIVE_WORDS = {"si", "sí", "yes", "ok", "dale", "va", "claro", "quiero", "manda", "mandame",
                  "mandalo", "por favor", "porfavor", "porfa", "bueno", "perfecto", "genial",
                  "me interesa", "interesa", "vamos", "1", "👍", "🙌"}


def _is_positive(text: str) -> bool:
    """Check if the SMS response is positive."""
    clean = text.strip().lower().replace("!", "").replace(".", "").replace(",", "")
    # Check exact match first
    if clean in POSITIVE_WORDS:
        return True
    # Check if any positive word is contained
    for word in POSITIVE_WORDS:
        if word in clean:
            return True
    return False


@router.post("/incoming")
async def sms_incoming(request: Request):
    """Twilio SMS webhook — receives incoming SMS replies."""
    form = await request.form()
    from_number = form.get("From", "")
    body = form.get("Body", "")
    to_number = form.get("To", "")

    logger.info("sms_incoming from=%s body=%s", from_number, body[:100])

    if not from_number or not body:
        return Response(content="<Response></Response>", media_type="application/xml")

    # Normalize phone
    phone = from_number.replace("whatsapp:", "").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    is_positive = _is_positive(body)
    logger.info("sms_response from=%s positive=%s body=%s", phone, is_positive, body[:50])

    if is_positive:
        # Process in background — don't block the webhook
        import asyncio
        asyncio.ensure_future(_handle_positive_reply(phone, body))
        # Reply immediately
        reply = "🎟️ ¡Genial! Te estoy generando tu boleto gratis para Beyond Wealth Miami. Te llega en unos segundos!"
    else:
        reply = "Gracias por tu respuesta. Si cambias de opinión, responde SI y te mandamos tu boleto gratis. 😊"

    # TwiML response
    twiml = f'<Response><Message>{reply}</Message></Response>'
    return Response(content=twiml, media_type="application/xml")


async def _handle_positive_reply(phone: str, body: str):
    """Generate ticket and enqueue for spartan upsell."""
    try:
        from supabase import create_client
        sb = create_client(settings.supabase_url, settings.supabase_service_role_key)

        campaign_id = DEFAULT_CAMPAIGN_ID

        # Find or create lead
        r = sb.table("leads").select("*").eq("campaign_id", campaign_id).or_(
            f"phone.eq.{phone},whatsapp.eq.{phone}"
        ).limit(1).execute()
        lead = (r.data or [None])[0]

        if not lead:
            # Try without + prefix
            phone_alt = phone[1:] if phone.startswith("+") else phone
            r = sb.table("leads").select("*").eq("campaign_id", campaign_id).or_(
                f"phone.eq.{phone_alt},whatsapp.eq.{phone_alt}"
            ).limit(1).execute()
            lead = (r.data or [None])[0]

        if not lead:
            logger.warning("sms_lead_not_found phone=%s", phone)
            # Send ticket anyway via SMS
            await _send_ticket_sms_only(phone, campaign_id)
            return

        lead_id = lead["lead_id"]
        name = lead.get("name", "")
        logger.info("sms_positive_lead found=%s name=%s", lead_id, name)

        # Update lead status
        sb.table("leads").update({"status": "GENERAL_CONFIRMED"}).eq("lead_id", lead_id).execute()

        # Generate ticket
        from ..services.tickets import generate_ticket_png
        campaign_r = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
        campaign = (campaign_r.data or [{}])[0]

        event_facts = {
            "event_id": campaign.get("id", ""),
            "event_name": campaign.get("event_name", "Beyond Wealth Miami"),
            "event_date": str(campaign.get("event_date", "2026-03-27")),
            "event_place": campaign.get("event_location", "EB Hotel Miami"),
            "event_speakers": campaign.get("event_speakers", "Spencer Hoffmann"),
        }

        ticket = generate_ticket_png(
            lead={"name": name or "Invitado", "email": lead.get("email", ""), "whatsapp": phone, "lead_id": lead_id},
            tier="GENERAL",
            event=event_facts,
        )
        ticket_url = f"https://calls-mx.fly.dev/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
        logger.info("sms_ticket_generated lead=%s ticket=%s", lead_id, ticket["ticket_id"])

        # Send ticket via WhatsApp template first, SMS fallback
        ticket_sent = False

        # Try WhatsApp template
        try:
            twilio_sid = campaign.get("twilio_account_sid", "")
            twilio_token = campaign.get("twilio_auth_token", "")
            twilio_from = campaign.get("twilio_whatsapp_from", "")
            if twilio_sid and twilio_token and twilio_from:
                import httpx
                wa_to = f"whatsapp:{phone}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                        auth=(twilio_sid, twilio_token),
                        data={
                            "From": twilio_from,
                            "To": wa_to,
                            "ContentSid": "HX5b3eab4955f93d3d4699478a07c51351",
                            "ContentVariables": f'{{"1":"{name or \"Invitado\"}","2":"Beyond Wealth Miami","3":"27-29 Marzo 2026","4":"{ticket_url}"}}',
                        },
                    )
                    if resp.status_code == 201:
                        ticket_sent = True
                        logger.info("sms_ticket_wa_ok lead=%s", lead_id)
        except Exception as e:
            logger.warning("sms_ticket_wa_fail lead=%s err=%s", lead_id, str(e)[:100])

        # SMS fallback
        if not ticket_sent:
            try:
                import httpx
                twilio_sid = campaign.get("twilio_account_sid") or "ACcfbfaa84e1a092be65596efbab6af33a"
                twilio_token = campaign.get("twilio_auth_token") or "b321f1dfe70dc7463d651008acbca9dc"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                        auth=(twilio_sid, twilio_token),
                        data={
                            "From": "+18885564279",
                            "To": phone,
                            "Body": f"🎟️ {name or 'Hola'}! Tu boleto GRATIS para Beyond Wealth Miami esta listo.\n\n📅 27-29 Marzo | EB Hotel Miami\n\n👉 {ticket_url}\n\nGuardalo y presentalo en la entrada. Te esperamos!",
                        },
                    )
                    if resp.status_code == 201:
                        ticket_sent = True
                        logger.info("sms_ticket_sms_ok lead=%s", lead_id)
            except Exception as e:
                logger.warning("sms_ticket_sms_fail lead=%s err=%s", lead_id, str(e)[:100])

        # Enqueue for spartan VIP upsell
        try:
            sb.table("call_queue").insert({
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "status": "pending",
                "call_type": "spartan",
                "purpose": "sell_vip",
                "priority": 2,
            }).execute()
            logger.info("sms_enqueued_spartan lead=%s", lead_id)
        except Exception as e:
            logger.warning("sms_enqueue_fail lead=%s err=%s", lead_id, str(e)[:100])

        # Log touchpoint
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "channel": "sms",
            "event_type": "sms_positive_reply",
            "payload": {"body": body[:200], "ticket_sent": ticket_sent, "ticket_id": ticket.get("ticket_id", "")},
        }).execute()

    except Exception as e:
        logger.exception("sms_handle_positive_failed phone=%s err=%s", phone, str(e)[:200])


async def _send_ticket_sms_only(phone: str, campaign_id: str):
    """Send a generic ticket via SMS when lead is not found."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://api.twilio.com/2010-04-01/Accounts/ACcfbfaa84e1a092be65596efbab6af33a/Messages.json",
                auth=("ACcfbfaa84e1a092be65596efbab6af33a", "b321f1dfe70dc7463d651008acbca9dc"),
                data={
                    "From": "+18885564279",
                    "To": phone,
                    "Body": "🎟️ Hola! Para registrarte a Beyond Wealth Miami (27-29 Marzo, GRATIS), visita: https://calls-mx.fly.dev/v1/landing/bw-miami\n\nEB Hotel Miami, Miami Springs FL. Te esperamos!",
                },
            )
            logger.info("sms_generic_ticket_sent phone=%s", phone)
    except Exception as e:
        logger.warning("sms_generic_ticket_fail phone=%s err=%s", phone, str(e)[:100])
