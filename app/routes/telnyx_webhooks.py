from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

from ..deps import sb
from ..settings import settings
from ..services.call_queue import complete_call, update_call_record
from ..services.post_call_processor import process_ai_call_outcome
from ..services.telnyx_calls import _campaign_telnyx, hangup_call, start_media_streaming, start_recording
from ..services.number_pool import record_call_result

logger = logging.getLogger("telnyx_webhooks")

router = APIRouter(prefix="/v1/calls/telnyx", tags=["telnyx-webhooks"])


# ─── Helpers ────────────────────────────────────────────────────────────────


def _decode_client_state(raw: str | None) -> dict:
    """Decode base64-encoded client_state from Telnyx webhook."""
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {}


def _encode_client_state(data: dict) -> str:
    """Encode dict as base64 for Telnyx client_state."""
    return base64.b64encode(json.dumps(data).encode("utf-8")).decode("utf-8")


def _resolve_campaign_by_number(phone: str) -> dict | None:
    """Find active campaign by telnyx_from_number or phone_numbers pool."""
    if not phone:
        return None
    normalized = phone.lstrip("+")
    for candidate in [f"+{normalized}", normalized]:
        # First: check campaigns.telnyx_from_number (legacy)
        try:
            r = (
                sb.table("campaigns")
                .select("*")
                .eq("telnyx_from_number", candidate)
                .eq("status", "active")
                .limit(1)
                .execute()
            )
            camp = (r.data or [None])[0]
            if camp:
                return camp
        except Exception:
            pass

        # Second: check phone_numbers pool table
        try:
            pn_r = (
                sb.table("phone_numbers")
                .select("campaign_id")
                .eq("number", candidate)
                .neq("status", "retired")
                .limit(1)
                .execute()
            )
            pn = (pn_r.data or [None])[0]
            if pn and pn.get("campaign_id"):
                return _resolve_campaign_by_id(pn["campaign_id"])
        except Exception:
            pass
    return None


def _resolve_campaign_by_id(campaign_id: str) -> dict | None:
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
    except Exception:
        return None


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
    except Exception:
        return None


def _touchpoint_exists(event_id: str) -> bool:
    """Check if a touchpoint with this telnyx event_id already exists (idempotency)."""
    if not event_id:
        return False
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("channel", "voice")
            .contains("payload", {"telnyx_event_id": event_id})
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


def _log_touchpoint(
    lead_id: str,
    event_type: str,
    campaign_id: str | None,
    payload: dict,
    event_id: str = "",
) -> None:
    """Insert a touchpoint row. Skips if event_id already logged (idempotent)."""
    if event_id and _touchpoint_exists(event_id):
        logger.debug("touchpoint_already_exists event_id=%s", event_id)
        return
    try:
        if event_id:
            payload["telnyx_event_id"] = event_id
        row: dict[str, Any] = {
            "lead_id": lead_id,
            "channel": "voice",
            "event_type": event_type,
            "payload": payload,
        }
        if campaign_id:
            row["campaign_id"] = campaign_id
        sb.table("touchpoints").insert(row).execute()
    except Exception as exc:
        logger.error(
            "touchpoint_insert_failed lead=%s event=%s err=%s",
            lead_id,
            event_type,
            str(exc)[:300],
        )


def _update_spartan_session_stats(
    campaign_id: str, caller_id: str | None, duration_seconds: int
) -> None:
    """Increment calls_today and talk_time_today_seconds on the spartan's active session."""
    if not caller_id or not campaign_id:
        return
    try:
        r = (
            sb.table("spartan_sessions")
            .select("id,calls_today,talk_time_today_seconds")
            .eq("campaign_id", campaign_id)
            .eq("user_id", caller_id)
            .neq("status", "offline")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        session = (r.data or [None])[0]
        if not session:
            return
        sb.table("spartan_sessions").update(
            {
                "calls_today": (session.get("calls_today") or 0) + 1,
                "talk_time_today_seconds": (session.get("talk_time_today_seconds") or 0) + duration_seconds,
                "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", session["id"]).execute()
        logger.info(
            "spartan_session_stats_updated session=%s caller=%s duration=%d",
            session["id"],
            caller_id,
            duration_seconds,
        )
    except Exception as exc:
        logger.error(
            "spartan_session_stats_failed caller=%s err=%s",
            caller_id,
            str(exc)[:300],
        )


# ─── Hangup-cause mapping ──────────────────────────────────────────────────

HANGUP_CAUSE_MAP: dict[str, str] = {
    "normal_clearing": "answered",
    "originator_cancel": "no_answer",
    "timeout": "no_answer",
    "no_answer": "no_answer",
    "user_busy": "busy",
    "busy": "busy",
    "call_rejected": "busy",
    "unallocated_number": "failed",
    "network_out_of_order": "failed",
    "normal_temporary_failure": "failed",
    "recovery_on_timer_expire": "no_answer",
}


def _map_hangup_cause(cause: str) -> str:
    """Map a Telnyx hangup_cause to a queue result string."""
    return HANGUP_CAUSE_MAP.get((cause or "").lower(), "failed")


# ─── Event handlers ────────────────────────────────────────────────────────


def _handle_call_initiated(
    event_id: str,
    payload: dict,
    client_state: dict,
    path_campaign_id: str | None,
) -> None:
    """Handle call.initiated — log only, no DB action needed."""
    call_control_id = payload.get("call_control_id", "")
    from_number = payload.get("from", "")
    to_number = payload.get("to", "")
    direction = payload.get("direction", "")

    logger.info(
        "call_initiated call=%s from=%s to=%s dir=%s",
        call_control_id,
        from_number,
        to_number,
        direction,
    )


async def _handle_call_answered(
    event_id: str,
    payload: dict,
    client_state: dict,
    path_campaign_id: str | None,
) -> None:
    """Handle call.answered — update call_record, start streaming/recording for AI calls."""
    call_control_id = payload.get("call_control_id", "")
    from_number = payload.get("from", "")
    to_number = payload.get("to", "")
    now = datetime.now(timezone.utc).isoformat()

    campaign_id = client_state.get("campaign_id") or path_campaign_id or ""
    queue_id = client_state.get("queue_id", "")
    lead_id = client_state.get("lead_id", "")
    caller_type = client_state.get("caller_type", "spartan")

    logger.info(
        "call_answered call=%s campaign=%s lead=%s caller_type=%s",
        call_control_id,
        campaign_id,
        lead_id,
        caller_type,
    )

    # Update call_record
    record = _find_call_record(call_control_id)
    if record:
        try:
            update_call_record(record["id"], {"status": "answered"})
        except Exception as exc:
            logger.error(
                "call_record_update_failed call=%s err=%s",
                call_control_id,
                str(exc)[:300],
            )

    # For AI calls: start media streaming and recording
    if caller_type == "ai":
        # Resolve API key from campaign
        campaign = _resolve_campaign_by_id(campaign_id) if campaign_id else None
        telnyx_creds = _campaign_telnyx(campaign)
        api_key = telnyx_creds.get("telnyx_api_key", "")

        # Build WebSocket URL for media streaming
        base_url = (settings.public_base_url or "").rstrip("/")
        if base_url:
            ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
            stream_url = f"{ws_url}/v1/calls/media-stream/{call_control_id}"
            asyncio.create_task(
                start_media_streaming(call_control_id, telnyx_api_key=api_key, stream_url=stream_url)
            )

        # Start recording
        asyncio.create_task(start_recording(call_control_id, telnyx_api_key=api_key))

    # Log touchpoint
    if lead_id:
        _log_touchpoint(
            lead_id=lead_id,
            event_type="call_answered",
            campaign_id=campaign_id or None,
            payload={
                "call_control_id": call_control_id,
                "from": from_number,
                "to": to_number,
                "caller_type": caller_type,
            },
            event_id=event_id,
        )


async def _handle_call_hangup(
    event_id: str,
    payload: dict,
    client_state: dict,
    path_campaign_id: str | None,
) -> None:
    """Handle call.hangup — update call_record, complete queue, log touchpoint."""
    call_control_id = payload.get("call_control_id", "")
    hangup_cause = payload.get("hangup_cause", "")
    from_number = payload.get("from", "")
    to_number = payload.get("to", "")
    now = datetime.now(timezone.utc).isoformat()

    campaign_id = client_state.get("campaign_id") or path_campaign_id or ""
    queue_id = client_state.get("queue_id", "")
    lead_id = client_state.get("lead_id", "")
    caller_type = client_state.get("caller_type", "spartan")

    # Compute duration
    start_time = payload.get("start_time", "")
    end_time = payload.get("end_time", "")
    duration_seconds = 0
    if start_time and end_time:
        try:
            st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_seconds = max(0, int((et - st).total_seconds()))
        except Exception:
            pass

    result = _map_hangup_cause(hangup_cause)

    logger.info(
        "call_hangup call=%s cause=%s result=%s duration=%d lead=%s",
        call_control_id,
        hangup_cause,
        result,
        duration_seconds,
        lead_id,
    )

    # Track number pool health
    if from_number and campaign_id:
        try:
            await record_call_result(from_number, campaign_id, result)
        except Exception as exc:
            logger.error("record_call_result_failed from=%s err=%s", from_number, str(exc)[:200])

    # Update call_record
    record = _find_call_record(call_control_id)
    if record:
        try:
            update_call_record(
                record["id"],
                {
                    "status": "completed",
                    "duration_seconds": duration_seconds,
                },
            )
        except Exception as exc:
            logger.error(
                "call_record_update_failed call=%s err=%s",
                call_control_id,
                str(exc)[:300],
            )

    # Complete queue entry
    if queue_id:
        try:
            complete_call(queue_id=queue_id, result=result)
        except Exception as exc:
            logger.error(
                "queue_complete_failed queue=%s err=%s",
                queue_id,
                str(exc)[:300],
            )

    # Log touchpoint
    if lead_id:
        _log_touchpoint(
            lead_id=lead_id,
            event_type="call_completed",
            campaign_id=campaign_id or None,
            payload={
                "call_control_id": call_control_id,
                "from": from_number,
                "to": to_number,
                "hangup_cause": hangup_cause,
                "result": result,
                "duration_seconds": duration_seconds,
                "caller_type": caller_type,
            },
            event_id=event_id,
        )

    # Update spartan session stats if applicable
    if caller_type == "spartan" and record:
        caller_id = record.get("caller_id")
        _update_spartan_session_stats(campaign_id, caller_id, duration_seconds)

    # Backup: trigger post-call processing for AI calls
    # Primary trigger is in call_media_ws.py; delayed to let WS handler save conversation log first
    if caller_type == "ai" and record:
        async def _delayed_post_call(rec_id: str):
            await asyncio.sleep(10)  # Let WS handler save conversation log
            await process_ai_call_outcome(rec_id)

        asyncio.create_task(
            _delayed_post_call(record["id"]),
            name=f"post_call_backup_{record['id']}",
        )


def _handle_recording_saved(
    event_id: str,
    payload: dict,
    client_state: dict,
    path_campaign_id: str | None,
) -> None:
    """Handle call.recording.saved — store recording URL on call_record."""
    call_control_id = payload.get("call_control_id", "")
    recording_urls = payload.get("recording_urls", {})
    mp3_url = recording_urls.get("mp3", "")

    logger.info("recording_saved call=%s url=%s", call_control_id, mp3_url)

    if not mp3_url:
        return

    record = _find_call_record(call_control_id)
    if record:
        try:
            update_call_record(record["id"], {"recording_url": mp3_url})
        except Exception as exc:
            logger.error(
                "recording_update_failed call=%s err=%s",
                call_control_id,
                str(exc)[:300],
            )


async def _handle_amd(
    event_id: str,
    payload: dict,
    client_state: dict,
    path_campaign_id: str | None,
) -> None:
    """Handle call.machine.detection.ended — hang up on machines."""
    call_control_id = payload.get("call_control_id", "")
    amd_result = payload.get("result", "not_sure")

    campaign_id = client_state.get("campaign_id") or path_campaign_id or ""
    queue_id = client_state.get("queue_id", "")
    lead_id = client_state.get("lead_id", "")

    logger.info("amd_result call=%s result=%s", call_control_id, amd_result)

    if amd_result == "machine":
        # Hang up — this is a voicemail
        campaign = _resolve_campaign_by_id(campaign_id) if campaign_id else None
        telnyx_creds = _campaign_telnyx(campaign)
        api_key = telnyx_creds.get("telnyx_api_key", "")

        await hangup_call(call_control_id, telnyx_api_key=api_key)

        # Track voicemail in number pool health
        from_number = payload.get("from", "")
        if from_number and campaign_id:
            try:
                await record_call_result(from_number, campaign_id, "voicemail")
            except Exception:
                pass

        # Complete queue as voicemail
        if queue_id:
            try:
                complete_call(queue_id=queue_id, result="voicemail")
            except Exception as exc:
                logger.error(
                    "queue_complete_voicemail_failed queue=%s err=%s",
                    queue_id,
                    str(exc)[:300],
                )

        # Update call_record
        record = _find_call_record(call_control_id)
        if record:
            try:
                update_call_record(
                    record["id"],
                    {"status": "completed", "outcome": "voicemail"},
                )
            except Exception as exc:
                logger.error(
                    "call_record_amd_update_failed call=%s err=%s",
                    call_control_id,
                    str(exc)[:300],
                )

        if lead_id:
            _log_touchpoint(
                lead_id=lead_id,
                event_type="call_completed",
                campaign_id=campaign_id or None,
                payload={
                    "call_control_id": call_control_id,
                    "amd_result": amd_result,
                    "result": "voicemail",
                },
                event_id=event_id,
            )


def _handle_streaming_event(
    event_type: str,
    event_id: str,
    payload: dict,
) -> None:
    """Handle streaming.started / streaming.stopped — log only."""
    call_control_id = payload.get("call_control_id", "")
    suffix = "started" if "started" in event_type else "stopped"
    logger.info("streaming_%s call=%s", suffix, call_control_id)


# ─── Webhook endpoints ─────────────────────────────────────────────────────


@router.post("/webhooks")
@router.post("/webhooks/{campaign_id}")
async def telnyx_webhook(request: Request, campaign_id: str | None = None) -> Response:
    """
    Receive Telnyx call-control webhooks.

    Always returns 200 OK to prevent Telnyx from retrying.
    """
    try:
        body = await request.json()
    except Exception:
        logger.warning("telnyx_webhook_invalid_json")
        return Response(status_code=200, content="ok")

    data = body.get("data", {})
    event_type = data.get("event_type", "")
    event_id = data.get("id", "")
    payload = data.get("payload", {})

    # Decode client_state
    raw_client_state = payload.get("client_state", "")
    client_state = _decode_client_state(raw_client_state)

    # If no campaign from client_state or path, try resolving by "to" number
    path_campaign_id = campaign_id
    if not client_state.get("campaign_id") and not path_campaign_id:
        to_number = payload.get("to", "")
        if to_number:
            campaign = _resolve_campaign_by_number(to_number)
            if campaign:
                path_campaign_id = campaign.get("id")

    logger.info(
        "telnyx_webhook event=%s event_id=%s call=%s",
        event_type,
        event_id,
        payload.get("call_control_id", ""),
    )

    try:
        if event_type == "call.initiated":
            _handle_call_initiated(event_id, payload, client_state, path_campaign_id)

        elif event_type == "call.answered":
            await _handle_call_answered(event_id, payload, client_state, path_campaign_id)

        elif event_type == "call.hangup":
            await _handle_call_hangup(event_id, payload, client_state, path_campaign_id)

        elif event_type == "call.recording.saved":
            _handle_recording_saved(event_id, payload, client_state, path_campaign_id)

        elif event_type == "call.machine.detection.ended":
            await _handle_amd(event_id, payload, client_state, path_campaign_id)

        elif event_type in ("streaming.started", "streaming.stopped"):
            _handle_streaming_event(event_type, event_id, payload)

        else:
            logger.debug("telnyx_webhook_unhandled event=%s", event_type)

    except Exception as exc:
        # Never let exceptions propagate — always return 200 to Telnyx
        logger.exception(
            "telnyx_webhook_handler_error event=%s err=%s",
            event_type,
            str(exc)[:500],
        )

    return Response(status_code=200, content="ok")
