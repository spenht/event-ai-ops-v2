from __future__ import annotations

import json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request

from ..deps import sb
from ..settings import settings
from ..services.tickets import generate_ticket_png
from ..services.twilio_whatsapp import send_whatsapp

logger = logging.getLogger("payments")

router = APIRouter(prefix="/v1/payments", tags=["payments"])


def _event_facts(event_id: str | None) -> dict:
    # Minimal (ENV overrides DB)
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


@router.post("/create-link")
async def create_link(payload: dict):
    """Create a Stripe Checkout session for VIP.

    payload: {lead_id, event_id, tier, price_id, success_url, cancel_url}
    """
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")

    stripe.api_key = settings.stripe_secret_key

    lead_id = (payload.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(status_code=400, detail="lead_id required")

    lead_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
    lead = (lead_res.data or [None])[0]
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    event_id = (payload.get("event_id") or lead.get("event_id") or "").strip() or None
    tier = (payload.get("tier") or "VIP").strip().upper()

    price_id = (payload.get("price_id") or settings.stripe_vip_price_id).strip()
    if not price_id:
        raise HTTPException(status_code=500, detail="Missing STRIPE_VIP_PRICE_ID")

    success_url = (payload.get("success_url") or settings.stripe_success_url or "").strip()
    cancel_url = (payload.get("cancel_url") or settings.stripe_cancel_url or "").strip()
    if not success_url or not cancel_url:
        raise HTTPException(status_code=500, detail="Missing STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            customer_email=lead.get("email") or None,
            metadata={
                "lead_id": lead_id,
                "event_id": event_id or "",
                "tier": tier,
                "whatsapp": lead.get("whatsapp") or "",
            },
        )
    except Exception as e:
        logger.exception("stripe_create_session_failed %s", str(e)[:300])
        raise HTTPException(status_code=500, detail="stripe create session failed")

    try:
        sb.table("touchpoints").insert(
            {
                "lead_id": lead_id,
                "channel": "stripe",
                "event_type": "checkout_created",
                "payload": {"session_id": session.id, "url": session.url, "tier": tier, "event_id": event_id},
            }
        ).execute()
    except Exception:
        pass

    return {"url": session.url, "session_id": session.id}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise HTTPException(status_code=500, detail="Missing stripe config")

    stripe.api_key = settings.stripe_secret_key

    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(raw, sig, settings.stripe_webhook_secret)
    except Exception as e:
        logger.error("stripe_webhook_signature_invalid %s", str(e)[:300])
        raise HTTPException(status_code=400, detail="invalid signature")

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    # Persist webhook receipt
    try:
        sb.table("touchpoints").insert(
            {
                "lead_id": (obj.get("metadata") or {}).get("lead_id") or f"stripe:{obj.get('id')}",
                "channel": "stripe",
                "event_type": "stripe_webhook",
                "payload": {"type": etype, "id": obj.get("id")},
            }
        ).execute()
    except Exception:
        pass

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        lead_id = (meta.get("lead_id") or "").strip()
        tier = (meta.get("tier") or "VIP").strip().upper()
        event_id = (meta.get("event_id") or "").strip() or None

        if lead_id:
            # Mark paid
            try:
                sb.table("leads").update({"payment_status": "PAID", "status": f"{tier}_PAID"}).eq("lead_id", lead_id).execute()
            except Exception:
                pass

            # Generate ticket + send
            lead_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
            lead = (lead_res.data or [None])[0] or {}

            wa = (lead.get("whatsapp") or meta.get("whatsapp") or "").strip()
            if wa:
                facts = _event_facts(event_id or lead.get("event_id"))
                ticket = generate_ticket_png(lead=lead, tier=tier, event=facts)
                if not settings.public_base_url:
                    # Best effort; still send without media
                    msg = "✅ Pago recibido. Ya quedaste como VIP.\n\n(Nota: falta PUBLIC_BASE_URL para mandar el QR automático.)"
                    try:
                        await send_whatsapp(wa, msg)
                    except Exception:
                        pass
                else:
                    media = f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
                    msg = (
                        "✅ ¡Listo! Pago confirmado.\n"
                        "Aquí esta tu boleto VIP con tu QR (guardalo).\n\n"
                        "Si quieres, dime: ¿que te urge destrabar en el evento para que valga cada minuto?"
                    )
                    try:
                        await send_whatsapp(wa, msg, media_urls=[media])
                    except Exception as e:
                        logger.error("send_ticket_failed %s", str(e)[:300])

                    # Mark ticket as sent so whatsapp handler doesn't re-send
                    try:
                        sb.table("touchpoints").insert(
                            {
                                "lead_id": lead_id,
                                "channel": "whatsapp",
                                "event_type": "ticket_sent",
                                "payload": {"tier": tier, "ticket_id": ticket["ticket_id"], "source": "stripe_webhook"},
                            }
                        ).execute()
                    except Exception:
                        pass

                    # Send testimonials video (once)
                    testimonial_url = (settings.whatsapp_video_testimonios or "").strip() if hasattr(settings, "whatsapp_video_testimonios") else ""
                    if testimonial_url and testimonial_url.startswith("https://"):
                        try:
                            await send_whatsapp(wa, "🎬 Mira lo que dicen quienes ya vivieron Beyond Wealth 👇", media_urls=[testimonial_url])
                            sb.table("touchpoints").insert(
                                {
                                    "lead_id": lead_id,
                                    "channel": "whatsapp",
                                    "event_type": "media_sent",
                                    "payload": {"key": "testimonios", "url": testimonial_url},
                                }
                            ).execute()
                        except Exception:
                            pass

                        # Closing message from Ana
                        try:
                            event_name = facts.get("event_name") or "Beyond Wealth"
                            closing = (
                                "Soy Ana y me da muchisimo gusto poderte servir 😊\n\n"
                                f"Estoy muy emocionada de que vayas a ser parte de *{event_name}*, "
                                "un evento que puede cambiar tu vida.\n\n"
                                "Cualquier pregunta que tengas, aqui estoy para servirte."
                            ).strip()
                            await send_whatsapp(wa, closing)
                        except Exception:
                            pass

    return {"ok": True}
