from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.call_queue import (
    assign_call,
    complete_call,
    get_next_call,
    create_call_record,
    update_call_record,
    heartbeat_session,
)
from ..services.telnyx_calls import (
    generate_webrtc_credential,
    _campaign_telnyx,
    dial_outbound,
    _encode_client_state,
)
from ..services.number_pool import pick_number, detect_lead_country

logger = logging.getLogger("webrtc_api")

router = APIRouter(prefix="/v1/calls/webrtc", tags=["webrtc"])


def _normalize_phone_for_voice(phone: str) -> str:
    """Normalize phone number for Telnyx voice calls.

    Mexican WhatsApp numbers use +521XXXXXXXXXX but Telnyx voice
    requires +52XXXXXXXXXX (without the extra 1 after country code).
    """
    if phone.startswith("+521") and len(phone) == 14:
        return "+52" + phone[4:]
    return phone


# ─── Auth helper ─────────────────────────────────────────────────────────────

def _validate_auth(request: Request, campaign_id: str | None = None) -> None:
    """
    Validate request authentication.

    Checks X-Cron-Token header against global cron_token,
    or campaign-specific spartans_key.
    """
    token = (request.headers.get("x-cron-token") or "").strip()
    spartans_key = (request.headers.get("x-spartans-key") or "").strip()

    # Global cron token
    if settings.cron_token and token == settings.cron_token:
        return

    # Campaign-specific spartans key
    if campaign_id and spartans_key:
        try:
            r = (
                sb.table("campaigns")
                .select("spartans_key")
                .eq("id", campaign_id)
                .limit(1)
                .execute()
            )
            campaign = (r.data or [None])[0]
            if campaign and campaign.get("spartans_key") == spartans_key:
                return
        except Exception:
            pass

    # No token configured = open (dev mode)
    if not settings.cron_token:
        return

    raise HTTPException(status_code=403, detail="invalid auth token")


# ─── Request Models ──────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    campaign_id: str
    user_id: str


class DialRequest(BaseModel):
    campaign_id: str
    user_id: str
    session_id: str = ""
    queue_id: str = ""
    to_number: str = ""
    lead_id: str = ""


class CallEndedRequest(BaseModel):
    campaign_id: str
    user_id: str
    record_id: str
    queue_id: str = ""
    session_id: str = ""
    duration_seconds: int = 0
    result: str = "answered"
    outcome: str = ""
    notes: str = ""
    tags: list[str] = []


class AICallRequest(BaseModel):
    campaign_id: str
    lead_id: str
    queue_id: str = ""
    purpose: str = "confirm_attendance"


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/token")
async def webrtc_token(request: Request, body: TokenRequest):
    """Generate WebRTC SIP credentials for browser-based calling."""
    _validate_auth(request, body.campaign_id)

    # Fetch campaign for Telnyx creds
    try:
        r = (
            sb.table("campaigns")
            .select("telnyx_api_key, telnyx_sip_connection_id")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "token_campaign_fetch_failed campaign=%s err=%s",
            body.campaign_id,
            str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail="failed to fetch campaign")

    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")

    telnyx_api_key = (
        (campaign.get("telnyx_api_key") or "").strip()
        or settings.telnyx_api_key
    )
    connection_id = (
        (campaign.get("telnyx_sip_connection_id") or "").strip()
        or settings.telnyx_sip_connection_id
    )

    if not telnyx_api_key or not connection_id:
        raise HTTPException(
            status_code=400,
            detail="campaign missing telnyx_api_key or telnyx_sip_connection_id",
        )

    # Generate WebRTC credential via Telnyx
    try:
        cred = await generate_webrtc_credential(
            telnyx_api_key=telnyx_api_key,
            connection_id=connection_id,
        )
    except Exception as exc:
        logger.error(
            "webrtc_credential_failed campaign=%s err=%s",
            body.campaign_id,
            str(exc)[:300],
        )
        raise HTTPException(
            status_code=502, detail="failed to generate WebRTC credential"
        )

    logger.info(
        "webrtc_token_issued campaign=%s user=%s",
        body.campaign_id,
        body.user_id,
    )
    return {
        "sip_username": cred.get("sip_username", ""),
        "sip_password": cred.get("sip_password", ""),
        "sip_server": "sip.telnyx.com",
    }


@router.post("/dial")
async def webrtc_dial(request: Request, body: DialRequest):
    """
    Spartan initiates an outbound call via WebRTC.

    Returns the phone number and call record metadata so the browser
    can place the call using the @telnyx/webrtc SDK.
    """
    _validate_auth(request, body.campaign_id)

    lead_id = body.lead_id
    to_number = body.to_number
    lead_name = ""
    queue_id = body.queue_id

    # If queue_id provided, claim it and get lead info
    if queue_id:
        assigned = assign_call(queue_id, body.user_id)
        if not assigned:
            raise HTTPException(
                status_code=409,
                detail="call not available (already assigned or completed)",
            )
        lead_id = assigned.get("lead_id", lead_id)

        # Fetch lead details from queue entry
        try:
            lr = (
                sb.table("leads")
                .select("lead_id, name, phone, whatsapp")
                .eq("lead_id", lead_id)
                .limit(1)
                .execute()
            )
            lead = (lr.data or [None])[0]
            if lead:
                to_number = to_number or lead.get("phone") or lead.get("whatsapp") or ""
                # Strip whatsapp: prefix if present
                if to_number.startswith("whatsapp:"):
                    to_number = to_number[len("whatsapp:"):]
                to_number = _normalize_phone_for_voice(to_number)
                lead_name = lead.get("name") or ""
        except Exception as exc:
            logger.error(
                "dial_lead_fetch_failed lead=%s err=%s",
                lead_id,
                str(exc)[:300],
            )

    if not to_number:
        raise HTTPException(status_code=400, detail="no phone number to dial")

    # Fetch campaign for from_number and pool config
    try:
        cr = (
            sb.table("campaigns")
            .select("telnyx_from_number, number_pool_config, telnyx_api_key, telnyx_sip_connection_id")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception as exc:
        logger.error(
            "dial_campaign_fetch_failed campaign=%s err=%s",
            body.campaign_id,
            str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail="failed to fetch campaign")

    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")

    # Pick best number from pool (falls back to campaign.telnyx_from_number)
    lead_country = detect_lead_country(to_number)
    from_number = await pick_number(body.campaign_id, campaign, country=lead_country)

    # Create call record
    record = create_call_record(
        campaign_id=body.campaign_id,
        lead_id=lead_id,
        queue_id=queue_id or None,
        caller_type="spartan",
        caller_id=body.user_id,
        from_number=from_number,
        to_number=to_number,
        status="initiated",
    )
    if not record:
        raise HTTPException(status_code=500, detail="failed to create call record")

    record_id = record.get("id", "")

    logger.info(
        "webrtc_dial campaign=%s user=%s to=%s record=%s queue=%s",
        body.campaign_id,
        body.user_id,
        to_number,
        record_id,
        queue_id,
    )
    return {
        "record_id": record_id,
        "to_number": to_number,
        "from_number": from_number,
        "lead_id": lead_id,
        "lead_name": lead_name,
        "queue_id": queue_id,
    }


@router.post("/call-ended")
async def webrtc_call_ended(request: Request, body: CallEndedRequest):
    """Spartan reports that a WebRTC call has ended."""
    _validate_auth(request, body.campaign_id)

    # Update call record
    updates: dict[str, Any] = {"status": "completed"}
    if body.duration_seconds:
        updates["duration_seconds"] = body.duration_seconds
    if body.outcome:
        updates["outcome"] = body.outcome
    if body.notes:
        updates["notes"] = body.notes
    if body.tags:
        updates["tags"] = body.tags

    result = update_call_record(body.record_id, updates)
    if not result:
        logger.warning(
            "call_ended_record_update_failed record=%s", body.record_id
        )

    # Complete the queue entry if applicable
    if body.queue_id:
        complete_call(
            queue_id=body.queue_id,
            result=body.result,
            outcome=body.outcome,
            notes=body.notes,
            tags=body.tags,
        )

    # Update spartan session stats if session_id provided
    if body.session_id:
        try:
            # Fetch current session to increment counters
            sr = (
                sb.table("spartan_sessions")
                .select("calls_today, talk_time_today_seconds")
                .eq("id", body.session_id)
                .limit(1)
                .execute()
            )
            session = (sr.data or [None])[0]
            if session:
                sb.table("spartan_sessions").update(
                    {
                        "calls_today": (session.get("calls_today") or 0) + 1,
                        "talk_time_today_seconds": (
                            (session.get("talk_time_today_seconds") or 0)
                            + body.duration_seconds
                        ),
                    }
                ).eq("id", body.session_id).execute()
        except Exception as exc:
            logger.error(
                "session_stats_update_failed session=%s err=%s",
                body.session_id,
                str(exc)[:300],
            )

        # Also send heartbeat to keep the session alive
        heartbeat_session(body.session_id)

    logger.info(
        "webrtc_call_ended campaign=%s user=%s record=%s result=%s duration=%ds",
        body.campaign_id,
        body.user_id,
        body.record_id,
        body.result,
        body.duration_seconds,
    )
    return {"ok": True}


class SummarizeRequest(BaseModel):
    record_id: str
    campaign_id: str


@router.post("/summarize")
async def summarize_call(request: Request, body: SummarizeRequest):
    """
    Generate a brief AI summary of a call using the call's notes, tags, duration.
    Stores the summary in the call_record's ai_summary field.
    """
    _validate_auth(request, body.campaign_id)

    import httpx

    # Fetch call record
    try:
        rr = (
            sb.table("call_records")
            .select("*")
            .eq("id", body.record_id)
            .limit(1)
            .execute()
        )
        record = (rr.data or [None])[0]
        if not record:
            raise HTTPException(status_code=404, detail="call record not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("summarize_fetch_record_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to fetch record")

    # Fetch campaign for OpenAI API key
    try:
        cr = (
            sb.table("campaigns")
            .select("openai_api_key")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    openai_key = (campaign or {}).get("openai_api_key") or settings.openai_api_key
    if not openai_key:
        return {"ok": False, "message": "no OpenAI API key configured"}

    # Fetch lead info
    lead_name = ""
    lead_id = record.get("lead_id", "")
    if lead_id:
        try:
            lr = (
                sb.table("leads")
                .select("name, whatsapp, status, tags")
                .eq("lead_id", lead_id)
                .limit(1)
                .execute()
            )
            lead = (lr.data or [None])[0]
            if lead:
                lead_name = lead.get("name") or lead.get("whatsapp") or ""
        except Exception:
            pass

    # Build context for AI
    duration = record.get("duration_seconds") or 0
    notes = record.get("notes") or ""
    outcome = record.get("outcome") or ""
    tags = record.get("tags") or []
    result = record.get("status") or ""
    to_number = record.get("to_number") or ""

    if not notes and not outcome and duration == 0:
        return {"ok": False, "message": "no call data to summarize"}

    prompt = f"""Generate a very brief summary (1-2 sentences max) of this sales call.
Be concise and focus on the key outcome.

Lead: {lead_name or to_number}
Duration: {duration} seconds
Result: {result}
Outcome: {outcome}
Tags: {', '.join(tags) if tags else 'none'}
Agent notes: {notes or 'none'}

Summary:"""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a concise call summarizer. Output only 1-2 short sentences.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 100,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            summary = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
    except Exception as exc:
        logger.error("summarize_openai_failed err=%s", str(exc)[:300])
        return {"ok": False, "message": "AI summary generation failed"}

    if summary:
        update_call_record(body.record_id, {"ai_summary": summary})

        # Also update the lead record with the call summary for future reference
        if lead_id:
            try:
                # Append to lead's existing notes/tags
                existing_lead = (
                    sb.table("leads")
                    .select("notes, tags")
                    .eq("lead_id", lead_id)
                    .limit(1)
                    .execute()
                )
                lead_data = (existing_lead.data or [None])[0]
                existing_notes = (lead_data or {}).get("notes") or ""
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                new_notes = (
                    f"{existing_notes}\n[{ts}] AI Summary: {summary}"
                    if existing_notes
                    else f"[{ts}] AI Summary: {summary}"
                ).strip()

                update_data: dict[str, Any] = {"notes": new_notes}

                # Merge tags
                if tags:
                    existing_tags = (lead_data or {}).get("tags") or []
                    if isinstance(existing_tags, list):
                        merged_tags = list(set(existing_tags + tags))
                        update_data["tags"] = merged_tags

                sb.table("leads").update(update_data).eq(
                    "lead_id", lead_id
                ).execute()
            except Exception as exc:
                logger.error(
                    "summarize_lead_update_failed lead=%s err=%s",
                    lead_id,
                    str(exc)[:300],
                )

    logger.info(
        "call_summarized record=%s summary_len=%d",
        body.record_id,
        len(summary),
    )
    return {"ok": True, "summary": summary}


@router.post("/ai-call")
async def webrtc_ai_call(request: Request, body: AICallRequest):
    """Trigger an AI-powered outbound call via Telnyx."""
    _validate_auth(request, body.campaign_id)

    # Fetch campaign (needs Telnyx creds, AI config)
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception as exc:
        logger.error(
            "ai_call_campaign_fetch_failed campaign=%s err=%s",
            body.campaign_id,
            str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail="failed to fetch campaign")

    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")

    telnyx_api_key = (campaign.get("telnyx_api_key") or "").strip()
    # For AI outbound calls, prefer Call Control App ID (stored in telnyx_webrtc_credential_id)
    # over SIP Connection ID (which is for WebRTC browser calls)
    connection_id = (
        (campaign.get("telnyx_webrtc_credential_id") or "").strip()
        or (campaign.get("telnyx_sip_connection_id") or "").strip()
    )
    from_number = (campaign.get("telnyx_from_number") or "").strip()

    if not telnyx_api_key or not connection_id:
        raise HTTPException(
            status_code=400,
            detail="campaign missing Telnyx credentials",
        )

    # Fetch lead
    try:
        lr = (
            sb.table("leads")
            .select("*")
            .eq("lead_id", body.lead_id)
            .limit(1)
            .execute()
        )
        lead = (lr.data or [None])[0]
    except Exception as exc:
        logger.error(
            "ai_call_lead_fetch_failed lead=%s err=%s",
            body.lead_id,
            str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail="failed to fetch lead")

    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    # Determine phone number
    phone = (lead.get("phone") or lead.get("whatsapp") or "").strip()
    if phone.startswith("whatsapp:"):
        phone = phone[len("whatsapp:"):]
    phone = _normalize_phone_for_voice(phone)

    if not phone:
        raise HTTPException(status_code=400, detail="lead has no phone number")

    # Build client_state for webhook correlation
    client_state = _encode_client_state(
        {
            "campaign_id": body.campaign_id,
            "queue_id": body.queue_id,
            "lead_id": body.lead_id,
            "caller_type": "ai",
            "purpose": body.purpose,
        }
    )

    webhook_url = (
        f"{settings.public_base_url}/v1/calls/telnyx/webhooks/{body.campaign_id}"
        if settings.public_base_url
        else ""
    )

    # Dial outbound via Telnyx (AMD disabled for AI calls — prevents false hangups)
    try:
        dial_result = await dial_outbound(
            to_number=phone,
            from_number=from_number,
            connection_id=connection_id,
            telnyx_api_key=telnyx_api_key,
            webhook_url=webhook_url,
            client_state=client_state,
            amd="",
        )
    except Exception as exc:
        logger.error(
            "ai_call_dial_failed campaign=%s lead=%s err=%s",
            body.campaign_id,
            body.lead_id,
            str(exc)[:300],
        )
        raise HTTPException(status_code=502, detail="failed to dial outbound call")

    call_control_id = dial_result.get("call_control_id", "")

    # Create call record
    record = create_call_record(
        campaign_id=body.campaign_id,
        lead_id=body.lead_id,
        queue_id=body.queue_id or None,
        caller_type="ai",
        from_number=from_number,
        to_number=phone,
        status="initiated",
        purpose=body.purpose,
        notes=f"purpose:{body.purpose}",
    )
    if not record:
        logger.error(
            "ai_call_record_create_failed campaign=%s lead=%s",
            body.campaign_id,
            body.lead_id,
        )
        raise HTTPException(status_code=500, detail="failed to create call record")

    record_id = record.get("id", "")

    # Update call record with Telnyx call_control_id
    if call_control_id:
        update_call_record(
            record_id,
            {"telnyx_call_control_id": call_control_id},
        )

    logger.info(
        "ai_call_initiated campaign=%s lead=%s phone=%s call_control_id=%s record=%s",
        body.campaign_id,
        body.lead_id,
        phone,
        call_control_id,
        record_id,
    )
    return {
        "ok": True,
        "call_control_id": call_control_id,
        "record_id": record_id,
    }
