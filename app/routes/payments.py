from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request

from ..deps import sb
from ..settings import settings
from ..services.tickets import generate_ticket_png
from ..services.twilio_whatsapp import send_whatsapp
from ..services.url_shortener import create_short_url

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
                        "Aqui esta tu boleto VIP con tu QR (guardalo).\n\n"
                        "Te voy a compartir un video con algunos testimonios para que veas la transformacion que te espera en Beyond Wealth."
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
                            await send_whatsapp(wa, "🎬 Te comparto un video con algunos testimonios para que veas la transformacion que te espera en Beyond Wealth 👇", media_urls=[testimonial_url])
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

                        # Closing message (no re-introduction)
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

                        # Log all webhook-sent messages as outbound_ai so the AI
                        # conversation history knows they were already delivered.
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
                                "payload": {"to": f"whatsapp:{wa}", "body": webhook_summary, "source": "stripe_webhook"},
                            }).execute()
                        except Exception:
                            pass

                        # Schedule calendar reminder for ~10 min later
                        try:
                            from urllib.parse import quote_plus
                            lead_name = (lead.get("name") or "").strip()
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

    return {"ok": True}
