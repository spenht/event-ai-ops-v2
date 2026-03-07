from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.call_queue import (
    enqueue_call,
    assign_call,
    complete_call,
    get_next_call,
    get_queue_stats,
    create_call_record,
    update_call_record,
    start_session,
    heartbeat_session,
    end_session,
    get_active_sessions,
)

logger = logging.getLogger("calls_api")

router = APIRouter(prefix="/v1/calls", tags=["calls"])


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

class EnqueueRequest(BaseModel):
    campaign_id: str
    lead_id: str
    call_type: str = "initial_contact"
    priority: int = 0
    scheduled_for: Optional[str] = None


class ClaimRequest(BaseModel):
    user_id: str
    campaign_id: Optional[str] = None


class CompleteRequest(BaseModel):
    result: str  # answered, no_answer, busy, voicemail, failed
    outcome: str = ""
    notes: str = ""
    tags: list[str] = []


class SessionStartRequest(BaseModel):
    campaign_id: str
    user_id: str


class CallRecordUpdate(BaseModel):
    status: Optional[str] = None
    duration_seconds: Optional[int] = None
    recording_url: Optional[str] = None
    recording_sid: Optional[str] = None
    transcript: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_sentiment: Optional[str] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None


# ═══════════════════════════════════════════════════════════════════════════════
# QUEUE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/queue")
async def list_queue(
    request: Request,
    campaign_id: str,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """List call queue entries for a campaign."""
    _validate_auth(request, campaign_id)

    try:
        q = (
            sb.table("call_queue")
            .select("*")
            .eq("campaign_id", campaign_id)
        )
        if status:
            q = q.eq("status", status)

        q = q.order("priority", desc=True).order("created_at", desc=False)
        q = q.range(offset, offset + limit - 1)
        r = q.execute()
        return {"data": r.data or [], "count": len(r.data or [])}
    except Exception as exc:
        logger.error("list_queue_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to list queue")


@router.post("/queue")
async def enqueue(request: Request, body: EnqueueRequest):
    """Add a call to the queue."""
    _validate_auth(request, body.campaign_id)

    result = enqueue_call(
        campaign_id=body.campaign_id,
        lead_id=body.lead_id,
        call_type=body.call_type,
        priority=body.priority,
        scheduled_for=body.scheduled_for,
    )
    if not result:
        raise HTTPException(status_code=500, detail="failed to enqueue call")
    return result


@router.post("/queue/next")
async def next_call(request: Request, campaign_id: str):
    """Get the next call to dial for a campaign."""
    _validate_auth(request, campaign_id)

    call = get_next_call(campaign_id)
    if not call:
        return {"data": None, "message": "no pending calls"}
    return {"data": call}


@router.post("/queue/{queue_id}/claim")
async def claim_call(queue_id: str, request: Request, body: ClaimRequest):
    """Spartan claims the next call from the queue."""
    _validate_auth(request, body.campaign_id)

    result = assign_call(queue_id, body.user_id)
    if not result:
        raise HTTPException(
            status_code=409,
            detail="call not available (already assigned or completed)",
        )
    return result


@router.post("/queue/{queue_id}/complete")
async def complete(queue_id: str, request: Request, body: CompleteRequest):
    """Mark a queued call as completed with result."""
    _validate_auth(request)

    result = complete_call(
        queue_id=queue_id,
        result=body.result,
        outcome=body.outcome,
        notes=body.notes,
        tags=body.tags,
    )
    if not result:
        raise HTTPException(status_code=404, detail="queue entry not found")
    return {"ok": True, "queue_id": queue_id, "result": body.result}


@router.delete("/queue/{queue_id}")
async def cancel_queued_call(queue_id: str, request: Request):
    """Cancel a pending queued call."""
    _validate_auth(request)

    try:
        r = (
            sb.table("call_queue")
            .update({"status": "cancelled", "result": "cancelled"})
            .eq("id", queue_id)
            .eq("status", "pending")
            .execute()
        )
        updated = (r.data or [None])[0]
        if not updated:
            raise HTTPException(
                status_code=404,
                detail="queue entry not found or not pending",
            )
        logger.info("call_cancelled queue=%s", queue_id)
        return {"ok": True, "queue_id": queue_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("cancel_failed queue=%s err=%s", queue_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to cancel")


# ═══════════════════════════════════════════════════════════════════════════════
# CALL RECORDS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/records")
async def list_records(
    request: Request,
    campaign_id: str,
    limit: int = 50,
    offset: int = 0,
):
    """List call records (history) for a campaign."""
    _validate_auth(request, campaign_id)

    try:
        r = (
            sb.table("call_records")
            .select("*")
            .eq("campaign_id", campaign_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return {"data": r.data or [], "count": len(r.data or [])}
    except Exception as exc:
        logger.error("list_records_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to list records")


@router.get("/records/{record_id}")
async def get_record(record_id: str, request: Request):
    """Get a single call record by ID."""
    _validate_auth(request)

    try:
        r = (
            sb.table("call_records")
            .select("*")
            .eq("id", record_id)
            .limit(1)
            .execute()
        )
        record = (r.data or [None])[0]
        if not record:
            raise HTTPException(status_code=404, detail="record not found")
        return record
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_record_failed id=%s err=%s", record_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to get record")


@router.post("/records")
async def create_record(request: Request, body: dict):
    """Create a new call record."""
    campaign_id = body.get("campaign_id", "")
    _validate_auth(request, campaign_id)

    result = create_call_record(
        campaign_id=campaign_id,
        lead_id=body.get("lead_id", ""),
        queue_id=body.get("queue_id"),
        caller_type=body.get("caller_type", "spartan"),
        caller_id=body.get("caller_id"),
        from_number=body.get("from_number", ""),
        to_number=body.get("to_number", ""),
        status=body.get("status", "initiated"),
    )
    if not result:
        raise HTTPException(status_code=500, detail="failed to create record")
    return result


@router.patch("/records/{record_id}")
async def patch_record(
    record_id: str, request: Request, body: CallRecordUpdate
):
    """Update fields on a call record (status, duration, transcript, etc)."""
    _validate_auth(request)

    updates: dict[str, Any] = {}
    for field in [
        "status",
        "duration_seconds",
        "recording_url",
        "recording_sid",
        "transcript",
        "ai_summary",
        "ai_sentiment",
        "outcome",
        "notes",
        "tags",
    ]:
        val = getattr(body, field, None)
        if val is not None:
            updates[field] = val

    if not updates:
        return {"ok": True, "message": "no updates"}

    result = update_call_record(record_id, updates)
    if not result:
        raise HTTPException(status_code=404, detail="record not found")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SPARTAN SESSION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/sessions/start")
async def session_start(request: Request, body: SessionStartRequest):
    """Start a new spartan calling session."""
    _validate_auth(request, body.campaign_id)

    result = start_session(body.campaign_id, body.user_id)
    if not result:
        raise HTTPException(
            status_code=500, detail="failed to start session"
        )
    return result


@router.post("/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: str, request: Request):
    """Update heartbeat for an active spartan session."""
    _validate_auth(request)

    result = heartbeat_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@router.post("/sessions/{session_id}/end")
async def session_end(session_id: str, request: Request):
    """End a spartan calling session."""
    _validate_auth(request)

    result = end_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="session not found")
    return result


@router.get("/sessions")
async def list_sessions(request: Request, campaign_id: str):
    """List active spartan sessions for a campaign."""
    _validate_auth(request, campaign_id)

    sessions = get_active_sessions(campaign_id)
    return {"data": sessions, "count": len(sessions)}


# ═══════════════════════════════════════════════════════════════════════════════
# STATS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/stats")
async def call_stats(request: Request, campaign_id: str):
    """Get call queue statistics for a campaign."""
    _validate_auth(request, campaign_id)

    queue_stats = get_queue_stats(campaign_id)

    # Also get call records summary
    try:
        r = (
            sb.table("call_records")
            .select("status, duration_seconds")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        records = r.data or []
        total_calls = len(records)
        total_duration = sum(
            (rec.get("duration_seconds") or 0) for rec in records
        )
        answered = sum(
            1 for rec in records if rec.get("status") == "completed"
        )
    except Exception:
        total_calls = 0
        total_duration = 0
        answered = 0

    # Active sessions count
    sessions = get_active_sessions(campaign_id)

    return {
        "queue": queue_stats,
        "records": {
            "total_calls": total_calls,
            "answered": answered,
            "total_duration_seconds": total_duration,
        },
        "active_spartans": len(sessions),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PHONE NUMBERS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/phone-numbers")
async def list_phone_numbers(request: Request, campaign_id: str):
    """List phone numbers for a campaign."""
    _validate_auth(request, campaign_id)

    try:
        r = (
            sb.table("phone_numbers")
            .select("*")
            .eq("campaign_id", campaign_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"data": r.data or [], "count": len(r.data or [])}
    except Exception as exc:
        logger.error("list_phones_failed err=%s", str(exc)[:300])
        raise HTTPException(
            status_code=500, detail="failed to list phone numbers"
        )
