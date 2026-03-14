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
from ..services.number_pool import (
    import_existing_numbers,
    import_selected_numbers,
    list_available_telnyx_numbers,
    purchase_number,
    release_number,
    get_pool_stats,
)

logger = logging.getLogger("calls_api")

router = APIRouter(prefix="/v1/calls", tags=["calls"])


# ─── Auth helper ─────────────────────────────────────────────────────────────

def _validate_auth(request: Request, campaign_id: str | None = None) -> None:
    """
    Validate request authentication.

    Checks (in order):
    1. X-Cron-Token header against global cron_token
    2. X-Spartans-Key header against campaign-specific spartans_key (DB)
    3. X-Spartans-Key header against global settings.spartans_key (fallback)
    4. No token configured = open (dev mode)
    """
    token = (request.headers.get("x-cron-token") or "").strip()
    spartans_key = (request.headers.get("x-spartans-key") or "").strip()

    # 1. Global cron token
    if settings.cron_token and token == settings.cron_token:
        return

    # 2. Campaign-specific spartans key (from DB)
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
            if campaign and campaign.get("spartans_key") and campaign["spartans_key"] == spartans_key:
                return
        except Exception:
            pass

    # 3. Global spartans_key fallback (covers campaigns without DB key)
    if spartans_key and settings.spartans_key and spartans_key == settings.spartans_key:
        return

    # 4. No token configured = open (dev mode)
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



@router.get("/stats/agent")
async def agent_stats(
    request: Request, campaign_id: str, user_id: str
):
    """Get today's call stats for a specific agent."""
    _validate_auth(request, campaign_id)

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    try:
        r = (
            sb.table("call_records")
            .select("status, duration_seconds, outcome")
            .eq("campaign_id", campaign_id)
            .eq("caller_id", user_id)
            .gte("created_at", today)
            .execute()
        )
        records = r.data or []
        total_calls = len(records)
        answered = sum(
            1
            for rec in records
            if (rec.get("duration_seconds") or 0) > 0
        )
        total_duration = sum(
            (rec.get("duration_seconds") or 0) for rec in records
        )
        answer_rate = (
            round((answered / total_calls) * 100) if total_calls > 0 else 0
        )

        return {
            "calls_today": total_calls,
            "answered_today": answered,
            "talk_time_today_seconds": total_duration,
            "answer_rate": answer_rate,
        }
    except Exception as exc:
        logger.error("agent_stats_failed err=%s", str(exc)[:300])
        raise HTTPException(
            status_code=500, detail="failed to get agent stats"
        )


@router.get("/stats/team")
async def team_stats(request: Request, campaign_id: str):
    """Get per-agent call stats for today — team leader view."""
    _validate_auth(request, campaign_id)

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    try:
        r = (
            sb.table("call_records")
            .select("caller_id, status, duration_seconds, outcome, created_at")
            .eq("campaign_id", campaign_id)
            .gte("created_at", today)
            .execute()
        )
        records = r.data or []
    except Exception as exc:
        logger.error("team_stats_records_failed err=%s", str(exc)[:300])
        raise HTTPException(
            status_code=500, detail="failed to get team stats"
        )

    # Group by caller_id
    agents: dict[str, dict] = {}
    for rec in records:
        cid = rec.get("caller_id") or "unknown"
        if cid not in agents:
            agents[cid] = {"calls": 0, "answered": 0, "talk_time": 0}
        agents[cid]["calls"] += 1
        if (rec.get("duration_seconds") or 0) > 0:
            agents[cid]["answered"] += 1
        agents[cid]["talk_time"] += rec.get("duration_seconds") or 0

    # Get active sessions
    sessions = get_active_sessions(campaign_id)
    active_agents = {s.get("user_id"): s for s in sessions}

    results = []
    for agent_id, astats in agents.items():
        session = active_agents.get(agent_id)
        results.append(
            {
                "user_id": agent_id,
                "calls_today": astats["calls"],
                "answered_today": astats["answered"],
                "talk_time_seconds": astats["talk_time"],
                "answer_rate": (
                    round((astats["answered"] / astats["calls"]) * 100)
                    if astats["calls"] > 0
                    else 0
                ),
                "is_online": agent_id in active_agents,
                "session_started_at": (
                    session.get("started_at") if session else None
                ),
            }
        )

    # Sort by calls_today desc
    results.sort(key=lambda x: x["calls_today"], reverse=True)
    return {"data": results, "count": len(results)}


@router.get("/stats/team/detailed")
async def team_stats_detailed(request: Request, campaign_id: str):
    """Detailed per-agent stats with recent calls — team leader dashboard."""
    _validate_auth(request, campaign_id)

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    # 1. Fetch ALL today's call_records for this campaign
    try:
        r = (
            sb.table("call_records")
            .select(
                "id, caller_id, lead_id, to_number, status, "
                "duration_seconds, outcome, notes, tags, "
                "ai_summary, ai_sentiment, created_at"
            )
            .eq("campaign_id", campaign_id)
            .gte("created_at", today)
            .order("created_at", desc=True)
            .execute()
        )
        records = r.data or []
    except Exception as exc:
        logger.error("team_detailed_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to get team stats")

    # 2. Group records by caller_id
    agents_map: dict[str, dict] = {}
    for rec in records:
        cid = rec.get("caller_id") or "unknown"
        if cid not in agents_map:
            agents_map[cid] = {
                "calls": 0,
                "answered": 0,
                "talk_time": 0,
                "tags_set": set(),
                "recent_calls": [],
            }
        a = agents_map[cid]
        a["calls"] += 1
        dur = rec.get("duration_seconds") or 0
        if dur > 0:
            a["answered"] += 1
        a["talk_time"] += dur
        for tag in rec.get("tags") or []:
            a["tags_set"].add(tag)
        if len(a["recent_calls"]) < 10:
            a["recent_calls"].append(rec)

    # 3. Get active sessions
    sessions = get_active_sessions(campaign_id)
    active_agents = {s.get("user_id"): s for s in sessions}

    # 4. Build agent results
    agent_results = []
    for agent_id, astats in agents_map.items():
        session = active_agents.get(agent_id)
        answer_rate = (
            round((astats["answered"] / astats["calls"]) * 100)
            if astats["calls"] > 0
            else 0
        )
        avg_dur = (
            round(astats["talk_time"] / astats["answered"])
            if astats["answered"] > 0
            else 0
        )
        agent_results.append(
            {
                "user_id": agent_id,
                "calls_today": astats["calls"],
                "answered_today": astats["answered"],
                "talk_time_seconds": astats["talk_time"],
                "answer_rate": answer_rate,
                "avg_duration_seconds": avg_dur,
                "is_online": agent_id in active_agents,
                "session_started_at": (
                    session.get("started_at") if session else None
                ),
                "tags_used": sorted(astats["tags_set"]),
                "tags_count": len(astats["tags_set"]),
                "recent_calls": astats["recent_calls"],
            }
        )

    agent_results.sort(key=lambda x: x["calls_today"], reverse=True)

    # 5. Build summary
    total_calls = sum(a["calls_today"] for a in agent_results)
    total_answered = sum(a["answered_today"] for a in agent_results)
    total_talk = sum(a["talk_time_seconds"] for a in agent_results)
    team_rate = (
        round((total_answered / total_calls) * 100) if total_calls > 0 else 0
    )

    # 6. Recent activity (last 20 records across all agents)
    recent_activity = records[:20]

    return {
        "summary": {
            "total_calls_today": total_calls,
            "total_answered_today": total_answered,
            "team_answer_rate": team_rate,
            "total_talk_time_seconds": total_talk,
            "active_agents": len(sessions),
        },
        "agents": agent_results,
        "recent_activity": recent_activity,
        "count": len(agent_results),
    }


@router.get("/stats/agent/daily-summary")
async def agent_daily_summary(
    request: Request, campaign_id: str, user_id: str
):
    """Generate an AI daily summary for an agent's performance today."""
    _validate_auth(request, campaign_id)

    from datetime import datetime, timezone
    import httpx

    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    # Fetch today's call records for this agent
    try:
        r = (
            sb.table("call_records")
            .select("duration_seconds, outcome, notes, tags, ai_summary, to_number, status")
            .eq("campaign_id", campaign_id)
            .eq("caller_id", user_id)
            .gte("created_at", today)
            .execute()
        )
        records = r.data or []
    except Exception as exc:
        logger.error("daily_summary_records_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to fetch records")

    if not records:
        return {
            "ok": True,
            "summary": "No calls made today yet. Start dialing to see your daily summary!",
            "stats": {"calls": 0, "answered": 0, "talk_time": 0},
        }

    # Calculate stats
    total_calls = len(records)
    answered = sum(1 for r in records if (r.get("duration_seconds") or 0) > 0)
    total_duration = sum((r.get("duration_seconds") or 0) for r in records)
    answer_rate = round((answered / total_calls * 100) if total_calls > 0 else 0)
    avg_duration = round(total_duration / answered) if answered > 0 else 0

    # Fetch campaign OpenAI key
    try:
        cr = (
            sb.table("campaigns")
            .select("openai_api_key")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    from ..settings import settings as app_settings
    openai_key = (campaign or {}).get("openai_api_key") or app_settings.openai_api_key
    if not openai_key:
        return {
            "ok": True,
            "summary": f"Today: {total_calls} calls, {answered} answered ({answer_rate}%), {total_duration // 60}m talk time.",
            "stats": {
                "calls": total_calls,
                "answered": answered,
                "talk_time": total_duration,
                "answer_rate": answer_rate,
            },
        }

    # Build call summaries for AI context
    call_summaries = []
    for i, rec in enumerate(records[:20], 1):
        dur = rec.get("duration_seconds") or 0
        notes = rec.get("notes") or ""
        outcome = rec.get("outcome") or ""
        ai_sum = rec.get("ai_summary") or ""
        tags = rec.get("tags") or []
        line = f"Call {i}: {dur}s"
        if tags:
            line += f", tags=[{', '.join(tags)}]"
        if outcome:
            line += f", outcome={outcome}"
        if ai_sum:
            line += f", summary={ai_sum}"
        elif notes:
            line += f", notes={notes}"
        call_summaries.append(line)

    prompt = f"""Analyze this agent's daily call performance and provide brief, actionable feedback.

Stats:
- Total calls: {total_calls}
- Answered: {answered} ({answer_rate}%)
- Total talk time: {total_duration // 60}m {total_duration % 60}s
- Avg call duration: {avg_duration}s

Call details:
{chr(10).join(call_summaries)}

Provide:
1. A 1-sentence summary of the day
2. One thing done well
3. One thing to improve
Keep it under 100 words total. Be encouraging but honest."""

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
                            "content": "You are a supportive sales coach reviewing a call agent's daily performance. Be concise and actionable.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 200,
                    "temperature": 0.5,
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
        logger.error("daily_summary_openai_failed err=%s", str(exc)[:300])
        summary = f"Today: {total_calls} calls, {answered} answered ({answer_rate}%), {total_duration // 60}m talk time."

    return {
        "ok": True,
        "summary": summary,
        "stats": {
            "calls": total_calls,
            "answered": answered,
            "talk_time": total_duration,
            "answer_rate": answer_rate,
            "avg_duration": avg_duration,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PHONE NUMBERS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# LEADS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/leads")
async def list_leads(
    request: Request,
    campaign_id: str,
    status: str = "",
    payment_status: str = "",
    source: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """List leads for a campaign with optional filters."""
    _validate_auth(request, campaign_id)

    try:
        q = (
            sb.table("leads")
            .select("*")
            .eq("campaign_id", campaign_id)
        )
        if status:
            q = q.eq("status", status)
        if payment_status:
            q = q.eq("payment_status", payment_status)
        if source:
            q = q.ilike("source", f"%{source}%")
        if search:
            q = q.or_(
                f"name.ilike.%{search}%,"
                f"email.ilike.%{search}%,"
                f"whatsapp.ilike.%{search}%,"
                f"lead_id.ilike.%{search}%"
            )

        q = q.order("created_at", desc=True)
        q = q.range(offset, offset + limit - 1)
        r = q.execute()

        # Get total count (separate query without pagination)
        count_q = (
            sb.table("leads")
            .select("lead_id", count="exact")
            .eq("campaign_id", campaign_id)
        )
        if status:
            count_q = count_q.eq("status", status)
        if payment_status:
            count_q = count_q.eq("payment_status", payment_status)
        count_r = count_q.execute()
        total = count_r.count if hasattr(count_r, "count") and count_r.count is not None else len(r.data or [])

        return {"data": r.data or [], "count": len(r.data or []), "total": total}
    except Exception as exc:
        logger.error("list_leads_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to list leads")


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: str, request: Request, campaign_id: str = ""):
    """Get a single lead by ID."""
    _validate_auth(request, campaign_id)

    try:
        q = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1)
        if campaign_id:
            q = q.eq("campaign_id", campaign_id)
        r = q.execute()
        lead = (r.data or [None])[0]
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        return lead
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_lead_failed id=%s err=%s", lead_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to get lead")


@router.patch("/leads/{lead_id}")
async def update_lead(lead_id: str, request: Request, body: dict):
    """Update lead fields."""
    campaign_id = body.pop("campaign_id", "")
    _validate_auth(request, campaign_id)

    # Only allow safe fields to be updated
    allowed = {
        "name", "email", "phone", "whatsapp", "status", "payment_status",
        "tier_interest", "source", "notes", "tags", "vip_count",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return {"ok": True, "message": "no updates"}

    try:
        r = sb.table("leads").update(updates).eq("lead_id", lead_id).execute()
        updated = (r.data or [None])[0]
        if not updated:
            raise HTTPException(status_code=404, detail="lead not found")
        return {"ok": True, "data": updated}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("update_lead_failed id=%s err=%s", lead_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to update lead")


@router.get("/leads/stats/summary")
async def leads_stats(request: Request, campaign_id: str):
    """Get lead statistics for a campaign."""
    _validate_auth(request, campaign_id)

    try:
        r = (
            sb.table("leads")
            .select("status, payment_status, tier_interest, source")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        leads = r.data or []
        total = len(leads)

        status_counts: dict[str, int] = {}
        payment_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        for lead in leads:
            s = lead.get("status") or "UNKNOWN"
            status_counts[s] = status_counts.get(s, 0) + 1
            ps = lead.get("payment_status") or "NONE"
            payment_counts[ps] = payment_counts.get(ps, 0) + 1
            src = (lead.get("source") or "unknown").split(":")[0]
            source_counts[src] = source_counts.get(src, 0) + 1

        return {
            "total": total,
            "by_status": status_counts,
            "by_payment_status": payment_counts,
            "by_source": source_counts,
        }
    except Exception as exc:
        logger.error("leads_stats_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to get lead stats")


# ═══════════════════════════════════════════════════════════════════════════════
# PHONE NUMBERS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/phone-numbers")
async def list_phone_numbers(request: Request, campaign_id: str):
    """List phone numbers for a campaign."""
    _validate_auth(request, campaign_id)

    try:
        # Try with health columns (after migration 011)
        try:
            r = (
                sb.table("phone_numbers")
                .select("*")
                .eq("campaign_id", campaign_id)
                .neq("status", "retired")
                .order("status", desc=False)
                .order("answer_rate", desc=True)
                .execute()
            )
        except Exception:
            # Fallback: order by created_at only (before migration)
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


@router.get("/phone-numbers/stats")
async def phone_numbers_stats(request: Request, campaign_id: str):
    """Get number pool statistics for a campaign."""
    _validate_auth(request, campaign_id)
    stats = await get_pool_stats(campaign_id)
    return {"ok": True, **stats}


class AddPhoneNumberRequest(BaseModel):
    campaign_id: str
    number: str
    country: str = "US"
    org_id: str = ""


@router.post("/phone-numbers")
async def add_phone_number(request: Request, body: AddPhoneNumberRequest):
    """Manually add a phone number to the pool."""
    _validate_auth(request, body.campaign_id)

    try:
        row = {
            "campaign_id": body.campaign_id,
            "org_id": body.org_id or None,
            "number": body.number.strip(),
            "country": body.country,
            "provider": "telnyx",
            "type": "local",
            "status": "active",
            "max_calls_per_day": 50,
        }
        r = sb.table("phone_numbers").insert(row).execute()
        return {"ok": True, "data": (r.data or [None])[0]}
    except Exception as exc:
        logger.error("add_phone_failed err=%s", str(exc)[:300])
        raise HTTPException(status_code=500, detail="failed to add phone number")


class SyncNumbersRequest(BaseModel):
    campaign_id: str
    org_id: str = ""


@router.post("/phone-numbers/sync")
async def sync_phone_numbers(request: Request, body: SyncNumbersRequest):
    """Import existing Telnyx numbers into the pool."""
    _validate_auth(request, body.campaign_id)

    # Fetch campaign for API key
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    imported = await import_existing_numbers(
        campaign_id=body.campaign_id,
        org_id=body.org_id,
        campaign=campaign,
    )
    return {"ok": True, "imported": len(imported), "numbers": imported}


@router.get("/phone-numbers/available")
async def available_telnyx_numbers(request: Request, campaign_id: str):
    """List all Telnyx numbers with their campaign assignment info."""
    _validate_auth(request, campaign_id)

    # Fetch campaign for API key
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    numbers = await list_available_telnyx_numbers(
        campaign_id=campaign_id,
        campaign=campaign,
    )
    return {"ok": True, "data": numbers, "count": len(numbers)}


class SyncSelectedRequest(BaseModel):
    campaign_id: str
    org_id: str = ""
    numbers: list[dict] = []  # [{phone_number, telnyx_id}, ...]


@router.post("/phone-numbers/sync-selected")
async def sync_selected_numbers(request: Request, body: SyncSelectedRequest):
    """Import selected Telnyx numbers into the pool."""
    _validate_auth(request, body.campaign_id)

    imported = await import_selected_numbers(
        campaign_id=body.campaign_id,
        org_id=body.org_id,
        telnyx_numbers=body.numbers,
    )
    return {"ok": True, "imported": len(imported), "numbers": imported}


class PurchaseNumberRequest(BaseModel):
    campaign_id: str
    org_id: str = ""
    country: str = "US"
    area_code: str = ""


@router.post("/phone-numbers/purchase")
async def purchase_phone_number(request: Request, body: PurchaseNumberRequest):
    """Purchase a new phone number from Telnyx and add to pool."""
    _validate_auth(request, body.campaign_id)

    # Fetch campaign for API key and connection ID
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    result = await purchase_number(
        campaign_id=body.campaign_id,
        org_id=body.org_id,
        campaign=campaign,
        country=body.country,
        area_code=body.area_code,
    )
    if not result:
        raise HTTPException(status_code=500, detail="failed to purchase number")

    return {"ok": True, "data": result}


@router.delete("/phone-numbers/{phone_number_id}")
async def delete_phone_number(request: Request, phone_number_id: str, campaign_id: str):
    """Retire a phone number (release from Telnyx and mark retired)."""
    _validate_auth(request, campaign_id)

    # Fetch campaign for API key
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception:
        campaign = None

    success = await release_number(
        phone_number_id=phone_number_id,
        campaign_id=campaign_id,
        campaign=campaign,
    )
    if not success:
        raise HTTPException(status_code=500, detail="failed to release number")

    return {"ok": True}


# ─── AI Call Diagnostics ─────────────────────────────────────────────────────


@router.get("/ai-calls/diagnostics")
async def ai_calls_diagnostics(request: Request, campaign_id: str):
    """Diagnostic endpoint to check AI call configuration for a campaign.

    Returns a checklist of all requirements for AI calls to work,
    highlighting which are passing and which are failing.
    """
    _validate_auth(request, campaign_id)

    from datetime import datetime, timedelta, timezone
    from ..services.delayed_call_scheduler import _resolve_telnyx_credentials

    checks: list[dict] = []
    now = datetime.now(timezone.utc)

    # 1. Campaign exists
    try:
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
    except Exception as exc:
        return {"ok": False, "error": f"Failed to fetch campaign: {str(exc)[:200]}"}

    checks.append({
        "check": "Campaign exists",
        "pass": bool(campaign),
        "detail": campaign.get("name", "") if campaign else "NOT FOUND",
    })
    if not campaign:
        return {"ok": False, "checks": checks}

    # 2. ai_calls_enabled
    ai_enabled = bool(campaign.get("ai_calls_enabled"))
    checks.append({
        "check": "ai_calls_enabled",
        "pass": ai_enabled,
        "detail": str(ai_enabled),
    })

    # 3. Telnyx API key
    api_key, conn_id = _resolve_telnyx_credentials(campaign)
    has_api_key = bool(api_key)
    api_key_source = "campaign" if (campaign.get("telnyx_api_key") or "").strip() else ("global" if api_key else "MISSING")
    checks.append({
        "check": "Telnyx API key",
        "pass": has_api_key,
        "detail": f"Source: {api_key_source}" + (f" ({api_key[:8]}...)" if api_key else ""),
    })

    # 4. Telnyx connection ID
    has_conn = bool(conn_id)
    conn_source = "campaign" if (
        (campaign.get("telnyx_webrtc_credential_id") or "").strip()
        or (campaign.get("telnyx_sip_connection_id") or "").strip()
    ) else ("global" if conn_id else "MISSING")
    checks.append({
        "check": "Telnyx connection ID",
        "pass": has_conn,
        "detail": f"Source: {conn_source}" + (f" ({conn_id[:12]}...)" if conn_id else ""),
    })

    # 5. From number available
    from_number = (campaign.get("telnyx_from_number") or "").strip()
    if not from_number:
        from_number = settings.telnyx_from_number
    checks.append({
        "check": "From number (fallback)",
        "pass": bool(from_number),
        "detail": from_number or "MISSING — need telnyx_from_number or number pool",
    })

    # 6. Number pool status
    try:
        pool_stats = await get_pool_stats(campaign_id)
        pool_active = pool_stats.get("active", 0)
        checks.append({
            "check": "Number pool",
            "pass": pool_active > 0 or bool(from_number),
            "detail": f"active={pool_active} warming={pool_stats.get('warming', 0)} (fallback={from_number or 'none'})",
        })
    except Exception:
        checks.append({
            "check": "Number pool",
            "pass": bool(from_number),
            "detail": f"Query failed, fallback={from_number or 'none'}",
        })

    # 7. Webhook URL
    has_webhook = bool(settings.public_base_url)
    checks.append({
        "check": "Webhook URL (PUBLIC_BASE_URL)",
        "pass": has_webhook,
        "detail": f"{settings.public_base_url}/v1/calls/telnyx/webhooks/{campaign_id}" if has_webhook else "MISSING",
    })

    # 8. Active spartan sessions (informational)
    active_agents = get_active_sessions(campaign_id)
    checks.append({
        "check": "Active spartan agents",
        "pass": True,
        "detail": f"{len(active_agents)} agent(s) online (AI calls proceed regardless)",
    })

    # 9. Eligible leads count
    try:
        eligible_r = (
            sb.table("leads")
            .select("lead_id", count="exact")
            .eq("campaign_id", campaign_id)
            .eq("status", "NEW")
            .neq("do_not_contact", True)
            .execute()
        )
        eligible_count = eligible_r.count or 0
        checks.append({
            "check": "Eligible NEW leads",
            "pass": eligible_count > 0,
            "detail": f"{eligible_count} leads in NEW status",
        })
    except Exception:
        checks.append({
            "check": "Eligible NEW leads",
            "pass": False,
            "detail": "Query failed",
        })

    # 10. Recent AI calls (last 24h)
    try:
        recent_r = (
            sb.table("call_records")
            .select("id", count="exact")
            .eq("campaign_id", campaign_id)
            .eq("caller_type", "ai")
            .gte("created_at", (now - timedelta(hours=24)).isoformat())
            .execute()
        )
        recent_count = recent_r.count or 0
        checks.append({
            "check": "Recent AI calls (24h)",
            "pass": True,
            "detail": f"{recent_count} AI calls in last 24 hours",
        })
    except Exception:
        checks.append({
            "check": "Recent AI calls (24h)",
            "pass": True,
            "detail": "Query failed",
        })

    # 11. ai_call_delay_minutes
    delay_min = campaign.get("ai_call_delay_minutes") or 10
    checks.append({
        "check": "AI call delay",
        "pass": True,
        "detail": f"{delay_min} minutes after lead event",
    })

    all_pass = all(c["pass"] for c in checks)
    return {
        "ok": all_pass,
        "campaign_id": campaign_id,
        "campaign_name": campaign.get("name", ""),
        "checks": checks,
    }
