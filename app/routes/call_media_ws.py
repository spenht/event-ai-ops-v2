from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..deps import sb
from ..settings import settings
from ..services.ai_voice import AIVoiceSession, build_voice_system_prompt
from ..services.call_queue import update_call_record
from ..services.post_call_processor import (
    check_whatsapp_window,
    process_ai_call_outcome,
)
from ..services.stripe_checkout import create_vip_checkout_link
from ..services.telnyx_calls import hangup_call
from ..services.tickets import generate_ticket_png
from ..services.twilio_whatsapp import send_whatsapp
from ..services.whatsapp_templates import send_whatsapp_template

logger = logging.getLogger("call_media_ws")

router = APIRouter(tags=["call-media"])


# ─── Active session registry (for monitoring / debug) ────────────────────────

_active_sessions: dict[str, AIVoiceSession] = {}


# ─── DB helpers ──────────────────────────────────────────────────────────────


def _find_call_record(call_control_id: str) -> dict | None:
    """Look up a call_record by its telnyx_call_control_id."""
    if not call_control_id:
        return None
    try:
        r = (
            sb.table("call_records")
            .select("*")
            .eq("telnyx_call_control_id", call_control_id)
            .limit(1)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "find_call_record_failed call=%s err=%s",
            call_control_id,
            str(exc)[:300],
        )
        return None


def _fetch_campaign(campaign_id: str) -> dict | None:
    """Fetch campaign by ID."""
    if not campaign_id:
        return None
    try:
        r = (
            sb.table("campaigns")
            .select("*")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "fetch_campaign_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:300],
        )
        return None


def _fetch_lead(lead_id: str) -> dict | None:
    """Fetch lead by lead_id."""
    if not lead_id:
        return None
    try:
        r = (
            sb.table("leads")
            .select("*")
            .eq("lead_id", lead_id)
            .limit(1)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "fetch_lead_failed lead=%s err=%s",
            lead_id,
            str(exc)[:300],
        )
        return None


def _fetch_event_facts(event_id: str) -> dict:
    """Fetch event facts by event_id."""
    if not event_id:
        return {}
    try:
        ev = (
            sb.table("events")
            .select("*")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        return (ev.data or [{}])[0] or {}
    except Exception:
        return {}


def _save_conversation_log(
    record_id: str, conversation_log: list[dict[str, str]]
) -> None:
    """Persist conversation log to the call_record."""
    if not record_id:
        return
    try:
        update_call_record(record_id, {"ai_conversation_log": conversation_log})
        logger.info(
            "conversation_log_saved record=%s turns=%d",
            record_id,
            len(conversation_log),
        )
    except Exception as exc:
        logger.error(
            "conversation_log_save_failed record=%s err=%s",
            record_id,
            str(exc)[:300],
        )


# ─── WebSocket endpoint ─────────────────────────────────────────────────────


@router.websocket("/v1/calls/media-stream/{call_control_id}")
async def media_stream(websocket: WebSocket, call_control_id: str):
    """Bidirectional audio bridge between Telnyx media streaming and OpenAI Realtime API.

    Lifecycle:
    1. Telnyx connects after streaming_start is called on the call.
    2. We create an AIVoiceSession for the call.
    3. Audio flows bidirectionally: Telnyx <-> OpenAI Realtime.
    4. On disconnect, conversation log is saved to the call_record.
    """
    await websocket.accept()
    logger.info("ws_accepted call=%s", call_control_id)

    session: AIVoiceSession | None = None
    record_id: str = ""
    watchdog_task: asyncio.Task | None = None

    try:
        # ── 1. Wait for the "connected" event from Telnyx ────────────────
        raw = await websocket.receive_text()
        first_msg = json.loads(raw)
        if first_msg.get("event") != "connected":
            logger.warning(
                "ws_unexpected_first_event call=%s event=%s",
                call_control_id,
                first_msg.get("event"),
            )
            await websocket.close(code=1008, reason="expected connected event")
            return

        stream_id = first_msg.get("stream_id", "")
        logger.info(
            "telnyx_connected call=%s stream=%s", call_control_id, stream_id
        )

        # ── 2. Look up call_record ───────────────────────────────────────
        call_record = _find_call_record(call_control_id)
        if not call_record:
            logger.error("ws_no_call_record call=%s", call_control_id)
            await websocket.close(code=1008, reason="call record not found")
            return

        record_id = call_record.get("id", "")
        campaign_id = call_record.get("campaign_id", "")
        lead_id = call_record.get("lead_id", "")

        # ── 3. Fetch campaign ────────────────────────────────────────────
        campaign = _fetch_campaign(campaign_id)
        if not campaign:
            logger.error(
                "ws_no_campaign call=%s campaign=%s",
                call_control_id,
                campaign_id,
            )
            await websocket.close(code=1008, reason="campaign not found")
            return

        # ── 4. Fetch lead ────────────────────────────────────────────────
        lead = _fetch_lead(lead_id) or {}

        # ── 5. Fetch event facts ─────────────────────────────────────────
        event_id = lead.get("event_id") or campaign.get("event_id") or ""
        event_facts = _fetch_event_facts(event_id)

        # ── 6. Build system prompt ───────────────────────────────────────
        # Read purpose: try DB column first, then parse from notes field
        call_purpose = (call_record.get("purpose") or "").strip()
        if not call_purpose:
            _notes = call_record.get("notes") or ""
            if _notes.startswith("purpose:"):
                call_purpose = _notes.split(":", 1)[1].strip()
        call_purpose = call_purpose or "confirm_attendance"

        # Auto-select purpose based on lead status when using default purpose
        if call_purpose == "confirm_attendance" and lead:
            _lead_status = (lead.get("status") or "").upper().strip()
            if _lead_status == "NEW" or (_lead_status == "" and not lead.get("name")):
                call_purpose = "complete_registration"
                logger.info(
                    "auto_purpose_override call=%s status=%s → complete_registration",
                    call_control_id, _lead_status,
                )

        logger.info("call_purpose call=%s purpose=%s", call_control_id, call_purpose)

        # ElevenLabs TTS (optional — per-campaign DB columns with smart fallback)
        # Voice config convention: ai_voice_name can be:
        #   "elevenlabs:<voice_id>"  → use ElevenLabs with that voice ID
        #   "shimmer" / "echo" etc.  → use OpenAI native voice (no ElevenLabs)
        #   empty/null               → use global defaults
        _raw_voice_name = (campaign.get("ai_voice_name") or "").strip()

        if _raw_voice_name.startswith("elevenlabs:"):
            # ElevenLabs voice — extract voice ID from ai_voice_name
            el_api_key = (campaign.get("elevenlabs_api_key") or "").strip() or settings.elevenlabs_api_key
            el_voice_id = _raw_voice_name.split(":", 1)[1].strip()
            el_model_id = (campaign.get("elevenlabs_model_id") or "").strip() or settings.elevenlabs_model_id
        elif _raw_voice_name:
            # OpenAI voice (shimmer, echo, etc.) — no ElevenLabs
            el_api_key = ""
            el_voice_id = ""
            el_model_id = ""
        else:
            # No voice config on campaign — use global defaults (backward compatible)
            el_api_key = settings.elevenlabs_api_key
            el_voice_id = settings.elevenlabs_voice_id
            el_model_id = settings.elevenlabs_model_id

        _use_elevenlabs = bool(el_api_key and el_voice_id)

        system_prompt = build_voice_system_prompt(
            campaign=campaign,
            lead=lead,
            event_facts=event_facts,
            purpose=call_purpose,
            use_elevenlabs=_use_elevenlabs,
        )

        # ── 7. Prepare callbacks ─────────────────────────────────────────

        _outbound_chunks = {"count": 0, "bytes": 0}

        async def send_audio_to_telnyx(audio_b64: bytes) -> None:
            """Send AI audio back through Telnyx WebSocket."""
            try:
                payload_str = audio_b64.decode("utf-8") if isinstance(audio_b64, bytes) else audio_b64
                await websocket.send_json({
                    "event": "media",
                    "media": {
                        "payload": payload_str,
                    },
                })
                _outbound_chunks["count"] += 1
                _outbound_chunks["bytes"] += len(payload_str)
                if _outbound_chunks["count"] == 1:
                    logger.info(
                        "first_outbound_audio call=%s payload_len=%d",
                        call_control_id,
                        len(payload_str),
                    )
                elif _outbound_chunks["count"] % 50 == 0:
                    logger.info(
                        "outbound_audio_progress call=%s chunks=%d total_b64_bytes=%d",
                        call_control_id,
                        _outbound_chunks["count"],
                        _outbound_chunks["bytes"],
                    )
            except Exception as exc:
                logger.error(
                    "send_to_telnyx_failed call=%s err=%s",
                    call_control_id,
                    str(exc)[:200],
                )

        async def on_transcript(role: str, text: str) -> None:
            """Log transcripts as they arrive."""
            logger.info(
                "transcript call=%s role=%s text=%s",
                call_control_id,
                role,
                text[:120],
            )

        # ── 8. Create AI voice session ───────────────────────────────────
        openai_key = (
            (campaign.get("openai_api_key") or "").strip()
            or settings.openai_api_key
        )
        ai_model = (campaign.get("ai_voice_model") or "").strip() or "gpt-4o-realtime-preview"
        # OpenAI voice — strip "elevenlabs:" prefix if present (those use ElevenLabs TTS, not OpenAI voice)
        _openai_voice_raw = (campaign.get("ai_voice_name") or "").strip()
        ai_voice = "alloy" if (not _openai_voice_raw or _openai_voice_raw.startswith("elevenlabs:")) else _openai_voice_raw
        ai_language = (campaign.get("ai_voice_language") or "").strip() or "es"

        lead_context: dict[str, Any] = {
            "name": lead.get("name", ""),
            "phone": lead.get("whatsapp", "") or lead.get("phone", ""),
            "status": lead.get("status", ""),
            "tier_interest": lead.get("tier_interest", ""),
            "email": lead.get("email", ""),
        }

        session = AIVoiceSession(
            openai_api_key=openai_key,
            model=ai_model,
            voice=ai_voice,
            system_prompt=system_prompt,
            lead_context=lead_context,
            event_facts=event_facts,
            language=ai_language,
            on_audio_delta=send_audio_to_telnyx,
            on_transcript=on_transcript,
            elevenlabs_api_key=el_api_key,
            elevenlabs_voice_id=el_voice_id,
            elevenlabs_model_id=el_model_id,
        )

        # Wire up end-call callback for AI-initiated hangup
        telnyx_api_key = (campaign.get("telnyx_api_key") or "").strip() or settings.telnyx_api_key

        async def _on_call_end() -> None:
            """AI requested call end — hang up the Telnyx call."""
            logger.info("ai_requested_hangup call=%s", call_control_id)
            try:
                await hangup_call(call_control_id, telnyx_api_key=telnyx_api_key)
            except Exception as exc:
                logger.error("ai_hangup_failed call=%s err=%s", call_control_id, str(exc)[:200])

        session.on_call_end = _on_call_end

        # Wire up VIP WhatsApp callback
        wa_number = lead.get("whatsapp") or lead.get("phone", "")

        async def _on_send_vip_whatsapp() -> dict:
            """AI requested to send VIP info via WhatsApp during the call."""
            logger.info("ai_send_vip_whatsapp call=%s lead=%s", call_control_id, lead_id)

            if not wa_number:
                return {"status": "error", "message": "No WhatsApp number for this lead"}

            window = check_whatsapp_window(lead_id)

            if window["can_send"]:
                # Within 24hr window — send payment link directly
                event_id_val = lead.get("event_id") or campaign.get("event_id") or ""
                checkout_url = await create_vip_checkout_link(
                    lead_id=lead_id,
                    event_id=event_id_val,
                    option=1,
                    stripe_secret_key=campaign.get("stripe_secret_key", ""),
                    stripe_price_ids=campaign.get("stripe_price_ids", ""),
                    success_url=campaign.get("stripe_success_url", ""),
                    cancel_url=campaign.get("stripe_cancel_url", ""),
                )
                lead_name_wa = lead.get("name", "")
                if checkout_url:
                    msg = (
                        f"¡Hola {lead_name_wa}! 🎉 Como platicamos por teléfono, "
                        f"aquí está tu link para asegurar tu lugar VIP:\n\n"
                        f"{checkout_url}\n\n"
                        f"¡Te esperamos! 🙌"
                    )
                    try:
                        await send_whatsapp(
                            wa_number, msg,
                            account_sid=campaign.get("twilio_account_sid", ""),
                            auth_token=campaign.get("twilio_auth_token", ""),
                            whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
                        )
                        # Update lead status
                        try:
                            sb.table("leads").update({"status": "VIP_LINK_SENT"}).eq("lead_id", lead_id).execute()
                        except Exception:
                            pass
                        # Log touchpoint
                        try:
                            sb.table("touchpoints").insert({
                                "lead_id": lead_id,
                                "channel": "whatsapp",
                                "event_type": "vip_link_sent_during_call",
                                "campaign_id": campaign_id,
                                "payload": {"call_control_id": call_control_id, "checkout_url": checkout_url[:80]},
                            }).execute()
                        except Exception:
                            pass
                        return {"status": "sent", "message": "Link de pago VIP enviado por WhatsApp"}
                    except Exception as exc:
                        logger.error("vip_wa_send_failed call=%s err=%s", call_control_id, str(exc)[:200])
                        return {"status": "error", "message": "Error al enviar WhatsApp"}
                else:
                    return {"status": "error", "message": "No se pudo crear el link de pago"}
            else:
                # Outside 24hr window — send template to prompt reply
                logger.info(
                    "vip_wa_outside_window call=%s hours=%s — sending template",
                    call_control_id, window.get("hours_since_last"),
                )
                # Look up approved template SID from broadcast_templates table
                content_sid = ""
                try:
                    tmpl_r = (
                        sb.table("broadcast_templates")
                        .select("content_sid, status")
                        .eq("name", "vip_call_prompt")
                        .limit(1)
                        .execute()
                    )
                    tmpl_row = (tmpl_r.data or [None])[0]
                    if tmpl_row:
                        content_sid = tmpl_row.get("content_sid", "")
                except Exception as exc:
                    logger.error("vip_template_lookup_failed err=%s", str(exc)[:200])

                lead_name_wa = lead.get("name", "")
                template_sent = False
                if content_sid:
                    try:
                        await send_whatsapp_template(
                            wa_number, content_sid, {"1": lead_name_wa or "Hola"},
                            account_sid=campaign.get("twilio_account_sid", ""),
                            auth_token=campaign.get("twilio_auth_token", ""),
                            whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
                        )
                        template_sent = True
                    except Exception as exc:
                        logger.error("vip_template_send_failed call=%s err=%s", call_control_id, str(exc)[:200])

                # Log pending VIP follow-up
                try:
                    sb.table("touchpoints").insert({
                        "lead_id": lead_id,
                        "channel": "voice",
                        "event_type": "vip_follow_up_pending",
                        "campaign_id": campaign_id,
                        "payload": {
                            "call_control_id": call_control_id,
                            "vip_option": 1,
                            "trigger": "ai_call_live",
                            "template_sent": template_sent,
                            "reason": "whatsapp_window_closed",
                        },
                    }).execute()
                except Exception:
                    pass

                if template_sent:
                    return {
                        "status": "template_sent",
                        "message": (
                            "Se envió un mensaje por WhatsApp. "
                            "Dile al lead que revise su WhatsApp y responda el mensaje "
                            "para que le puedas enviar el link de pago."
                        ),
                    }
                else:
                    return {
                        "status": "pending",
                        "message": (
                            "No se pudo enviar WhatsApp en este momento. "
                            "Dile al lead que cuando envíe un mensaje por WhatsApp "
                            "recibirá automáticamente el link de pago."
                        ),
                    }

        session.on_send_vip_whatsapp = _on_send_vip_whatsapp

        # ── send_payment_link callback ──
        async def _on_send_payment_link(option: int) -> dict:
            """Send Stripe checkout link for any tier option via WhatsApp."""
            logger.info("ai_send_payment_link call=%s lead=%s option=%d", call_control_id, lead_id, option)

            if not wa_number:
                return {"status": "error", "message": "No WhatsApp number for this lead"}

            window = check_whatsapp_window(lead_id)

            if window["can_send"]:
                event_id_val = lead.get("event_id") or campaign.get("event_id") or ""
                checkout_url = await create_vip_checkout_link(
                    lead_id=lead_id,
                    event_id=event_id_val,
                    option=option,
                    stripe_secret_key=campaign.get("stripe_secret_key", ""),
                    stripe_price_ids=campaign.get("stripe_price_ids", ""),
                    success_url=campaign.get("stripe_success_url", ""),
                    cancel_url=campaign.get("stripe_cancel_url", ""),
                    campaign_id=campaign_id,
                    whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
                )
                if checkout_url:
                    lead_name_wa = lead.get("name", "")
                    msg = (
                        f"Hola {lead_name_wa}! Como platicamos por telefono, "
                        f"aqui esta tu link de pago:\n\n"
                        f"{checkout_url}\n\n"
                        f"Te esperamos!"
                    )
                    try:
                        await send_whatsapp(
                            wa_number, msg,
                            account_sid=campaign.get("twilio_account_sid", ""),
                            auth_token=campaign.get("twilio_auth_token", ""),
                            whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
                        )
                        try:
                            sb.table("leads").update({"status": "VIP_LINK_SENT"}).eq("lead_id", lead_id).execute()
                        except Exception:
                            pass
                        try:
                            sb.table("touchpoints").insert({
                                "lead_id": lead_id,
                                "channel": "whatsapp",
                                "event_type": "payment_link_sent_during_call",
                                "campaign_id": campaign_id,
                                "payload": {"call_control_id": call_control_id, "option": option},
                            }).execute()
                        except Exception:
                            pass
                        return {"status": "sent", "message": "Link de pago enviado por WhatsApp"}
                    except Exception as exc:
                        logger.error("payment_link_wa_send_failed err=%s", str(exc)[:200])
                        return {"status": "error", "message": "Error al enviar WhatsApp"}
                else:
                    return {"status": "error", "message": "No se pudo crear el link de pago"}
            else:
                return {
                    "status": "pending",
                    "message": "No se pudo enviar WhatsApp (fuera de ventana 24h). Dile que escriba por WhatsApp.",
                }

        session.on_send_payment_link = _on_send_payment_link

        # ── check_payment_status callback ──
        async def _on_check_payment_status() -> dict:
            """Check if the lead has completed payment."""
            logger.info("ai_check_payment_status call=%s lead=%s", call_control_id, lead_id)
            try:
                lr = sb.table("leads").select("payment_status, status").eq("lead_id", lead_id).limit(1).execute()
                current = (lr.data or [{}])[0]
                payment_status = (current.get("payment_status") or "").upper()
                lead_status = (current.get("status") or "").upper()
                if payment_status == "PAID" or "PAID" in lead_status:
                    return {"status": "paid", "message": "El lead ya pago. Felicitalo y dile que su boleto le llegara por WhatsApp."}
                else:
                    return {"status": "pending", "message": "El pago aun no se refleja. Dile que se tome su tiempo."}
            except Exception as exc:
                logger.error("check_payment_status_failed err=%s", str(exc)[:200])
                return {"status": "error", "message": str(exc)[:100]}

        session.on_check_payment_status = _on_check_payment_status

        # ── send_ticket callback ──
        async def _on_send_ticket(tier: str) -> dict:
            """Generate and send a free ticket via WhatsApp."""
            logger.info("ai_send_ticket call=%s lead=%s tier=%s", call_control_id, lead_id, tier)

            if not wa_number:
                return {"status": "error", "message": "No WhatsApp number for this lead"}

            # Update lead status
            new_status = f"{tier.upper()}_CONFIRMED"
            try:
                sb.table("leads").update({"status": new_status}).eq("lead_id", lead_id).execute()
            except Exception:
                pass

            # Re-fetch lead for ticket gen
            try:
                lr = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
                current_lead = (lr.data or [lead])[0] or lead
            except Exception:
                current_lead = lead

            # Build event facts from campaign
            ticket_event_facts = {
                "event_id": campaign.get("event_id") or campaign.get("id") or "",
                "event_name": (campaign.get("event_name") or "Evento").strip(),
                "event_date": (str(campaign.get("event_date") or "")).strip(),
                "event_place": (campaign.get("event_location") or campaign.get("event_place") or "").strip(),
                "event_speakers": (campaign.get("event_speakers") or "").strip(),
            }
            ticket_config = campaign.get("ticket_config") if isinstance(campaign.get("ticket_config"), dict) else None

            try:
                ticket = generate_ticket_png(
                    lead=current_lead,
                    tier=tier.upper(),
                    event=ticket_event_facts,
                    ticket_config=ticket_config,
                    campaign_id=campaign_id,
                )
            except Exception as exc:
                logger.error("send_ticket_gen_failed err=%s", str(exc)[:200])
                return {"status": "error", "message": f"Error al generar boleto: {str(exc)[:80]}"}

            base_url = (
                (campaign.get("public_base_url") or "").strip()
                or (settings.public_base_url if hasattr(settings, "public_base_url") else "")
                or ""
            ).rstrip("/")

            if not base_url:
                return {"status": "error", "message": "No se puede enviar boleto (falta PUBLIC_BASE_URL)"}

            media_url = f"{base_url}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
            msg = (
                f"Aqui esta tu boleto {tier.upper()} con tu codigo QR. "
                f"Guardalo y presentalo en la entrada."
            )

            try:
                await send_whatsapp(
                    wa_number, msg, media_urls=[media_url],
                    account_sid=campaign.get("twilio_account_sid", ""),
                    auth_token=campaign.get("twilio_auth_token", ""),
                    whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
                )
                try:
                    sb.table("touchpoints").insert({
                        "lead_id": lead_id,
                        "channel": "whatsapp",
                        "event_type": "ticket_sent",
                        "campaign_id": campaign_id,
                        "payload": {"tier": tier.upper(), "ticket_id": ticket["ticket_id"], "source": "ai_call"},
                    }).execute()
                except Exception:
                    pass
                return {"status": "sent", "message": f"Boleto {tier.upper()} enviado por WhatsApp"}
            except Exception as exc:
                logger.error("send_ticket_wa_failed err=%s", str(exc)[:200])
                return {"status": "error", "message": "Error al enviar boleto por WhatsApp"}

        session.on_send_ticket = _on_send_ticket

        _active_sessions[call_control_id] = session

        logger.info(
            "ai_session_creating call=%s model=%s voice=%s lead=%s",
            call_control_id,
            ai_model,
            ai_voice,
            lead_id,
        )

        # ── 9. Connect to OpenAI Realtime API ────────────────────────────
        await session.connect()
        logger.info("ai_session_connected call=%s", call_control_id)

        # ── 9b. Duration watchdog — auto-hangup after max duration ────────
        max_duration = campaign.get("ai_call_max_duration_seconds") or 600  # 10 min default

        async def _duration_watchdog():
            """Auto-close the WebSocket after max duration to prevent runaway calls."""
            await asyncio.sleep(max_duration)
            logger.warning(
                "duration_watchdog_triggered call=%s max=%ds",
                call_control_id,
                max_duration,
            )
            try:
                await websocket.close(code=1000, reason="max duration reached")
            except Exception:
                pass

        watchdog_task = asyncio.create_task(
            _duration_watchdog(), name=f"watchdog_{call_control_id}"
        )

        # ── 10. Main loop: read from Telnyx, forward to OpenAI ───────────
        _inbound_chunks = {"count": 0}

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event", "")

            if event == "media":
                media = msg.get("media", {})
                track = media.get("track", "")
                # Telnyx uses "payload" for media data
                chunk = media.get("payload", "") or media.get("chunk", "")

                if _inbound_chunks["count"] == 0 and track == "inbound":
                    logger.info(
                        "first_inbound_media call=%s track=%s media_keys=%s chunk_len=%d",
                        call_control_id,
                        track,
                        list(media.keys()),
                        len(chunk),
                    )

                if track == "inbound" and chunk:
                    _inbound_chunks["count"] += 1
                    await session.send_audio(chunk)
                    if _inbound_chunks["count"] % 100 == 0:
                        logger.info(
                            "inbound_audio_progress call=%s chunks=%d",
                            call_control_id,
                            _inbound_chunks["count"],
                        )

            elif event == "stop":
                logger.info(
                    "telnyx_stop call=%s stream=%s",
                    call_control_id,
                    msg.get("stream_id", ""),
                )
                break

            else:
                logger.debug(
                    "telnyx_unhandled_event call=%s event=%s",
                    call_control_id,
                    event,
                )

    except WebSocketDisconnect:
        logger.info("ws_disconnected call=%s", call_control_id)

    except json.JSONDecodeError as exc:
        logger.error(
            "ws_json_error call=%s err=%s", call_control_id, str(exc)[:200]
        )

    except Exception as exc:
        logger.exception(
            "ws_error call=%s err=%s", call_control_id, str(exc)[:500]
        )

    finally:
        # ── Cleanup: close AI session, save log, remove from registry ────
        conversation_log: list[dict[str, str]] = []

        # Cancel watchdog
        if watchdog_task and not watchdog_task.done():
            watchdog_task.cancel()

        if session:
            try:
                conversation_log = await session.close()
            except Exception as exc:
                logger.error(
                    "ai_session_close_failed call=%s err=%s",
                    call_control_id,
                    str(exc)[:300],
                )
                # Try to grab whatever log exists
                conversation_log = session.conversation_log

        # Save conversation log to call_record
        if record_id and conversation_log:
            _save_conversation_log(record_id, conversation_log)

        # Trigger post-call processing (non-blocking)
        if record_id:
            asyncio.create_task(
                process_ai_call_outcome(record_id),
                name=f"post_call_{record_id}",
            )

        # Remove from active sessions registry
        _active_sessions.pop(call_control_id, None)

        # Close the WebSocket gracefully
        try:
            await websocket.close()
        except Exception:
            pass

        logger.info(
            "ws_cleanup_done call=%s turns=%d",
            call_control_id,
            len(conversation_log),
        )
