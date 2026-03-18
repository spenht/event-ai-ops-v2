from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ..deps import sb
from ..settings import settings
from .call_queue import update_call_record
from .stripe_checkout import create_vip_checkout_link
from .twilio_whatsapp import send_whatsapp

logger = logging.getLogger("post_call_processor")


# ─── Outcome extraction via GPT-4o-mini ─────────────────────────────────────


OUTCOME_EXTRACTION_PROMPT = """\
Eres un analista de conversaciones telefónicas. Analiza la siguiente conversación \
entre una IA (assistant) y un lead (user).

Retorna SOLO un JSON válido con esta estructura:
{
  "outcome": "<uno de los outcomes válidos>",
  "summary": "<resumen de 1-2 oraciones>",
  "tags": ["<tag1>", "<tag2>"],
  "vip_option": <1 o 2 o null>,
  "sentiment": "<positive|neutral|negative>"
}

Outcomes válidos:
- "confirmed" — El lead confirmó asistencia al evento
- "vip_interested" — El lead mostró interés en VIP pero no confirmó pago
- "vip_committed" — El lead dijo explícitamente que quiere pagar VIP
- "declined" — El lead dijo que no asistirá
- "callback_requested" — El lead pidió que lo llamen después
- "not_interested" — El lead no tiene interés
- "no_clear_outcome" — No se pudo determinar un resultado claro
- "wrong_number" — Número equivocado o persona incorrecta
- "voicemail" — Buzón de voz / no contestó

Tags posibles (usa todos los que apliquen):
- "confirmed_general", "vip_interested", "vip_committed"
- "positive_sentiment", "negative_sentiment"
- "asked_about_price", "asked_about_date", "asked_about_location"
- "will_bring_guest", "needs_more_info", "busy_now"

IMPORTANTE:
- Responde SOLO con el JSON, sin explicaciones.
- Si la conversación está vacía o tiene menos de 2 turnos, usa outcome="no_clear_outcome".
"""


async def extract_conversation_outcome(
    conversation_log: list[dict[str, str]],
    campaign: dict,
    lead: dict,
) -> dict[str, Any]:
    """Use GPT-4o-mini to analyze a conversation and extract structured outcome."""

    if not conversation_log or len(conversation_log) < 2:
        return {
            "outcome": "no_clear_outcome",
            "summary": "Conversación demasiado corta para analizar",
            "tags": [],
            "vip_option": None,
            "sentiment": "neutral",
        }

    # Build conversation text for analysis
    conv_text_parts: list[str] = []
    for turn in conversation_log:
        role = turn.get("role", "unknown")
        text = turn.get("text", "")
        label = "IA" if role == "assistant" else "Lead"
        conv_text_parts.append(f"{label}: {text}")
    conv_text = "\n".join(conv_text_parts)

    # Context
    lead_name = lead.get("name", "Desconocido")
    lead_status = lead.get("status", "")
    event_name = campaign.get("event_name") or campaign.get("name", "el evento")

    user_msg = (
        f"Conversación con {lead_name} (status actual: {lead_status}) "
        f"sobre {event_name}:\n\n{conv_text}"
    )

    api_key = (campaign.get("openai_api_key") or "").strip() or settings.openai_api_key
    if not api_key:
        logger.error("extract_outcome_no_api_key")
        return {
            "outcome": "no_clear_outcome",
            "summary": "No API key available",
            "tags": [],
            "vip_option": None,
            "sentiment": "neutral",
        }

    payload = {
        "model": "gpt-4o-mini",
        "input": [
            {"role": "system", "content": OUTCOME_EXTRACTION_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_output_tokens": 300,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                logger.error(
                    "extract_outcome_http_error status=%s body=%s",
                    resp.status_code,
                    resp.text[:500],
                )
                return {
                    "outcome": "no_clear_outcome",
                    "summary": f"API error: {resp.status_code}",
                    "tags": [],
                    "vip_option": None,
                    "sentiment": "neutral",
                }
            data = resp.json()

        # Extract text from response
        raw_text = ""
        if isinstance(data.get("output_text"), str):
            raw_text = data["output_text"].strip()
        else:
            for item in data.get("output", []) or []:
                for c in item.get("content", []) or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        raw_text = c["text"].strip()
                        break
                if raw_text:
                    break

        if not raw_text:
            logger.warning("extract_outcome_empty_response")
            return {
                "outcome": "no_clear_outcome",
                "summary": "Empty AI response",
                "tags": [],
                "vip_option": None,
                "sentiment": "neutral",
            }

        # Parse JSON — handle markdown code blocks
        clean = raw_text
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

        result = json.loads(clean)
        logger.info(
            "extract_outcome_ok outcome=%s summary=%s",
            result.get("outcome"),
            (result.get("summary") or "")[:80],
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("extract_outcome_json_parse err=%s raw=%s", str(exc)[:200], raw_text[:300])
        return {
            "outcome": "no_clear_outcome",
            "summary": "Failed to parse AI response",
            "tags": [],
            "vip_option": None,
            "sentiment": "neutral",
        }
    except Exception as exc:
        logger.exception("extract_outcome_error err=%s", str(exc)[:300])
        return {
            "outcome": "no_clear_outcome",
            "summary": f"Error: {str(exc)[:100]}",
            "tags": [],
            "vip_option": None,
            "sentiment": "neutral",
        }


# ─── WhatsApp 24-hour window check ──────────────────────────────────────────


def check_whatsapp_window(lead_id: str) -> dict[str, Any]:
    """Check if we can send a proactive WhatsApp message to this lead.

    WhatsApp Business API allows sending messages within 24 hours of the
    lead's last inbound message (the "24-hour window").

    Returns: {"can_send": bool, "hours_since_last": float|None}
    """
    if not lead_id:
        return {"can_send": False, "hours_since_last": None}

    try:
        r = (
            sb.table("touchpoints")
            .select("created_at")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", "inbound")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return {"can_send": False, "hours_since_last": None}

        last_inbound = datetime.fromisoformat(
            rows[0]["created_at"].replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        hours_since = (now - last_inbound).total_seconds() / 3600

        return {
            "can_send": hours_since <= 24.0,
            "hours_since_last": round(hours_since, 2),
        }

    except Exception as exc:
        logger.error(
            "whatsapp_window_check_failed lead=%s err=%s",
            lead_id,
            str(exc)[:300],
        )
        return {"can_send": False, "hours_since_last": None}


# ─── VIP follow-up handler ──────────────────────────────────────────────────


async def handle_vip_follow_up(
    lead: dict,
    campaign: dict,
    outcome: dict[str, Any],
    call_record_id: str,
) -> None:
    """Handle VIP follow-up after AI call detects interest.

    If within WhatsApp 24hr window: send VIP payment link via WhatsApp.
    If outside window: log a pending follow-up touchpoint.
    """
    lead_id = lead.get("lead_id", "")
    lead_name = lead.get("name", "")
    wa_number = lead.get("whatsapp") or lead.get("phone", "")
    event_id = lead.get("event_id") or campaign.get("event_id") or ""
    vip_option = outcome.get("vip_option") or 1

    if not wa_number:
        logger.warning("vip_follow_up_no_phone lead=%s", lead_id)
        return

    window = check_whatsapp_window(lead_id)

    if window["can_send"]:
        # Within 24hr window — send payment link via WhatsApp
        checkout_url = await create_vip_checkout_link(
            lead_id=lead_id,
            event_id=event_id,
            option=vip_option,
            stripe_secret_key=campaign.get("stripe_secret_key", ""),
            stripe_price_ids=campaign.get("stripe_price_ids", ""),
            success_url=campaign.get("stripe_success_url", ""),
            cancel_url=campaign.get("stripe_cancel_url", ""),
        )

        if not checkout_url:
            logger.error("vip_follow_up_checkout_failed lead=%s", lead_id)
            # Log pending even though we tried
            _log_vip_pending(lead_id, campaign.get("id", ""), call_record_id, outcome)
            return

        # Send WhatsApp message with VIP link
        msg = (
            f"¡Hola {lead_name}! 🎉 Como platicamos por teléfono, "
            f"aquí está tu link para asegurar tu lugar VIP:\n\n"
            f"{checkout_url}\n\n"
            f"¡Te esperamos! 🙌"
        )

        try:
            await send_whatsapp(
                wa_number,
                msg,
                account_sid=campaign.get("twilio_account_sid", ""),
                auth_token=campaign.get("twilio_auth_token", ""),
                whatsapp_from=campaign.get("twilio_whatsapp_from", ""),
            )
            logger.info("vip_follow_up_sent lead=%s url=%s", lead_id, checkout_url[:60])

            # Update lead status
            try:
                sb.table("leads").update(
                    {"status": "VIP_LINK_SENT", "last_contact_at": datetime.now(timezone.utc).isoformat()}
                ).eq("lead_id", lead_id).execute()
            except Exception as exc:
                logger.error("vip_follow_up_status_update_failed lead=%s err=%s", lead_id, str(exc)[:200])

            # Log touchpoint
            try:
                sb.table("touchpoints").insert({
                    "lead_id": lead_id,
                    "channel": "whatsapp",
                    "event_type": "vip_link_sent_post_call",
                    "campaign_id": campaign.get("id"),
                    "payload": {
                        "checkout_url": checkout_url,
                        "vip_option": vip_option,
                        "call_record_id": call_record_id,
                        "trigger": "ai_call_outcome",
                    },
                }).execute()
            except Exception:
                pass

        except Exception as exc:
            logger.error(
                "vip_follow_up_whatsapp_failed lead=%s err=%s",
                lead_id,
                str(exc)[:300],
            )
            _log_vip_pending(lead_id, campaign.get("id", ""), call_record_id, outcome)

    else:
        # Outside 24hr window — log pending follow-up
        logger.info(
            "vip_follow_up_pending lead=%s hours_since=%s",
            lead_id,
            window.get("hours_since_last"),
        )
        _log_vip_pending(lead_id, campaign.get("id", ""), call_record_id, outcome)


def _log_vip_pending(
    lead_id: str,
    campaign_id: str,
    call_record_id: str,
    outcome: dict[str, Any],
) -> None:
    """Log a pending VIP follow-up touchpoint for when the lead reopens the WA window."""
    try:
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "channel": "voice",
            "event_type": "vip_follow_up_pending",
            "campaign_id": campaign_id or None,
            "payload": {
                "call_record_id": call_record_id,
                "vip_option": outcome.get("vip_option") or 1,
                "outcome": outcome.get("outcome"),
                "trigger": "ai_call_outcome",
                "reason": "whatsapp_window_closed",
            },
        }).execute()
    except Exception as exc:
        logger.error(
            "vip_pending_log_failed lead=%s err=%s",
            lead_id,
            str(exc)[:200],
        )


# ─── Main entry point ───────────────────────────────────────────────────────


async def process_ai_call_outcome(call_record_id: str) -> None:
    """Main entry point: analyze AI call conversation and take follow-up actions.

    Called from call_media_ws.py (primary) and telnyx_webhooks.py (backup).
    """
    if not call_record_id:
        return

    logger.info("process_ai_call_outcome_start record=%s", call_record_id)

    # 1. Fetch call record
    try:
        r = (
            sb.table("call_records")
            .select("*")
            .eq("id", call_record_id)
            .limit(1)
            .execute()
        )
        record = (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "process_outcome_fetch_failed record=%s err=%s",
            call_record_id,
            str(exc)[:300],
        )
        return

    if not record:
        logger.warning("process_outcome_no_record id=%s", call_record_id)
        return

    # Skip if already processed (outcome is non-empty)
    existing_outcome = (record.get("outcome") or "").strip()
    if existing_outcome and existing_outcome != "":
        logger.info("process_outcome_already_done record=%s outcome=%s", call_record_id, existing_outcome)
        return

    # Skip if not an AI call
    if record.get("caller_type") != "ai":
        logger.debug("process_outcome_not_ai record=%s type=%s", call_record_id, record.get("caller_type"))
        return

    conversation_log = record.get("ai_conversation_log")
    if not conversation_log or not isinstance(conversation_log, list):
        logger.warning(
            "process_outcome_no_conversation record=%s",
            call_record_id,
        )
        # Still update the record so we don't re-process
        update_call_record(call_record_id, {
            "outcome": "no_clear_outcome",
            "ai_summary": "No conversation log available",
        })
        return

    # 2. Fetch campaign and lead
    campaign_id = record.get("campaign_id", "")
    lead_id = record.get("lead_id", "")

    campaign: dict = {}
    lead: dict = {}

    try:
        if campaign_id:
            cr = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
            campaign = (cr.data or [{}])[0] or {}
    except Exception as exc:
        logger.error("process_outcome_campaign_fetch err=%s", str(exc)[:200])

    try:
        if lead_id:
            lr = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
            lead = (lr.data or [{}])[0] or {}
    except Exception as exc:
        logger.error("process_outcome_lead_fetch err=%s", str(exc)[:200])

    # 3. Extract outcome via GPT-4o-mini
    outcome = await extract_conversation_outcome(conversation_log, campaign, lead)

    # 4. Update call_record with outcome (using existing columns)
    update_call_record(call_record_id, {
        "outcome": outcome.get("outcome", "no_clear_outcome"),
        "ai_summary": outcome.get("summary", ""),
        "tags": outcome.get("tags", []),
        "notes": json.dumps(outcome, ensure_ascii=False),  # Full JSON in notes
    })

    logger.info(
        "process_outcome_saved record=%s outcome=%s tags=%s",
        call_record_id,
        outcome.get("outcome"),
        outcome.get("tags"),
    )

    # 5. Update lead status/tags based on outcome
    _update_lead_from_outcome(lead_id, lead, outcome)

    # 6. If VIP interested/committed → trigger follow-up
    if outcome.get("outcome") in ("vip_interested", "vip_committed"):
        try:
            await handle_vip_follow_up(lead, campaign, outcome, call_record_id)
        except Exception as exc:
            logger.error(
                "vip_follow_up_error record=%s err=%s",
                call_record_id,
                str(exc)[:300],
            )

    # 7. Log touchpoint with outcome
    try:
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "channel": "voice",
            "event_type": "ai_call_outcome",
            "campaign_id": campaign_id or None,
            "payload": {
                "call_record_id": call_record_id,
                "outcome": outcome.get("outcome"),
                "summary": outcome.get("summary"),
                "tags": outcome.get("tags"),
                "sentiment": outcome.get("sentiment"),
            },
        }).execute()
    except Exception as exc:
        logger.error(
            "outcome_touchpoint_failed record=%s err=%s",
            call_record_id,
            str(exc)[:200],
        )

    logger.info("process_ai_call_outcome_done record=%s", call_record_id)


def _update_lead_from_outcome(
    lead_id: str,
    lead: dict,
    outcome: dict[str, Any],
) -> None:
    """Update lead status and tags based on AI call outcome."""
    if not lead_id:
        return

    outcome_type = outcome.get("outcome", "")
    now = datetime.now(timezone.utc).isoformat()

    updates: dict[str, Any] = {"last_contact_at": now}

    # Status mapping: only upgrade, never downgrade
    current_status = (lead.get("status") or "").upper()
    status_priority = {
        "NEW": 0,
        "GENERAL_CONFIRMED": 1,
        "VIP_INTERESTED": 2,
        "VIP_LINK_SENT": 3,
        "VIP_PAID": 4,
    }
    current_rank = status_priority.get(current_status, 0)

    new_status = None
    if outcome_type == "confirmed" and current_rank < 1:
        new_status = "GENERAL_CONFIRMED"
    elif outcome_type in ("vip_interested", "vip_committed") and current_rank < 2:
        new_status = "VIP_INTERESTED"

    if new_status:
        updates["status"] = new_status

    # Update tier_interest
    if outcome_type in ("vip_interested", "vip_committed"):
        updates["tier_interest"] = "VIP"
    elif outcome_type == "confirmed" and lead.get("tier_interest") == "NONE":
        updates["tier_interest"] = "GENERAL"

    try:
        sb.table("leads").update(updates).eq("lead_id", lead_id).execute()
        logger.info(
            "lead_updated_from_outcome lead=%s status=%s tier=%s",
            lead_id,
            updates.get("status", "(unchanged)"),
            updates.get("tier_interest", "(unchanged)"),
        )
    except Exception as exc:
        logger.error(
            "lead_update_from_outcome_failed lead=%s err=%s",
            lead_id,
            str(exc)[:200],
        )
