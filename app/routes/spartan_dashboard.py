from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..deps import sb
from ..settings import settings
from ..services.commission_engine import get_agent_earnings, get_leaderboard

logger = logging.getLogger("spartan_dashboard")

router = APIRouter(prefix="/v1/spartan", tags=["spartan-dashboard"])


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


class AICoachRequest(BaseModel):
    call_record_id: str
    campaign_id: str


# ─── AI Coach prompt ─────────────────────────────────────────────────────────


AI_COACH_PROMPT = """\
You are an expert sales coach reviewing a phone call between a spartan (human sales agent) \
and a lead. Analyze the conversation and provide actionable coaching feedback.

Return ONLY a valid JSON object with this structure:
{
  "feedback": "<2-3 sentence overall assessment>",
  "strengths": ["<strength 1>", "<strength 2>"],
  "improvements": ["<improvement 1>", "<improvement 2>"],
  "tip_of_the_day": "<one specific, actionable tip>"
}

Focus on:
- Opening and rapport building
- Handling objections
- Closing technique
- Tone and energy
- Product knowledge demonstration

IMPORTANT: Respond ONLY with the JSON, no explanations.
"""


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/metrics")
async def spartan_metrics(request: Request, campaign_id: str, user_id: str):
    """Combined metrics: call stats + earnings + session time."""
    _validate_auth(request, campaign_id)
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        # Call stats
        all_calls = (
            sb.table("call_records")
            .select("id, status, duration_seconds, created_at")
            .eq("campaign_id", campaign_id)
            .eq("caller_id", user_id)
            .eq("caller_type", "spartan")
            .execute()
        )
        calls_data = all_calls.data or []
        total_calls = len(calls_data)
        calls_today = sum(1 for c in calls_data if c.get("created_at", "") >= today_start)
        total_talk_time = sum(int(c.get("duration_seconds") or 0) for c in calls_data)
        talk_time_today = sum(
            int(c.get("duration_seconds") or 0)
            for c in calls_data
            if c.get("created_at", "") >= today_start
        )

        # Earnings
        earnings = await get_agent_earnings(user_id, campaign_id)

        # Active session
        session = (
            sb.table("spartan_sessions")
            .select("id, started_at, status, calls_today, talk_time_today_seconds")
            .eq("campaign_id", campaign_id)
            .eq("user_id", user_id)
            .neq("status", "offline")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        active_session = (session.data or [None])[0]

        return {
            "ok": True,
            "data": {
                "calls": {
                    "total": total_calls,
                    "today": calls_today,
                    "total_talk_time_seconds": total_talk_time,
                    "talk_time_today_seconds": talk_time_today,
                },
                "earnings": earnings,
                "session": active_session,
            },
        }
    except Exception as exc:
        logger.error(
            "spartan_metrics_failed user=%s campaign=%s err=%s",
            user_id, campaign_id, str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.get("/leaderboard")
async def enhanced_leaderboard(request: Request, campaign_id: str):
    """Enhanced leaderboard with earnings + call stats + online status."""
    _validate_auth(request, campaign_id)
    try:
        ranked = await get_leaderboard(campaign_id)

        # Add online status from spartan_sessions
        active_sessions = (
            sb.table("spartan_sessions")
            .select("user_id, status")
            .eq("campaign_id", campaign_id)
            .neq("status", "offline")
            .execute()
        )
        online_map = {s["user_id"]: s["status"] for s in (active_sessions.data or [])}

        for agent in ranked:
            agent["online_status"] = online_map.get(agent["agent_id"], "offline")

        return {"ok": True, "data": ranked}
    except Exception as exc:
        logger.error("enhanced_leaderboard_failed campaign=%s err=%s", campaign_id, str(exc)[:300])
        raise HTTPException(status_code=500, detail=str(exc)[:200])


@router.post("/ai-coach")
async def ai_coach(request: Request, body: AICoachRequest):
    """AI coaching feedback for a specific call."""
    _validate_auth(request, body.campaign_id)
    try:
        # Get call record with conversation log
        call = (
            sb.table("call_records")
            .select("ai_conversation_log, ai_summary, caller_id, lead_id")
            .eq("id", body.call_record_id)
            .eq("campaign_id", body.campaign_id)
            .limit(1)
            .execute()
        )
        record = (call.data or [None])[0]
        if not record:
            raise HTTPException(status_code=404, detail="call record not found")

        conversation_log = record.get("ai_conversation_log") or []
        ai_summary = record.get("ai_summary") or ""

        if not conversation_log and not ai_summary:
            return {
                "ok": True,
                "data": {
                    "feedback": "No conversation data available for coaching.",
                    "strengths": [],
                    "improvements": [],
                    "tip_of_the_day": "Always greet the lead with energy and enthusiasm!",
                },
            }

        # Build conversation text
        conv_parts: list[str] = []
        for turn in conversation_log:
            role = turn.get("role", "unknown")
            text = turn.get("text", "")
            label = "Agent" if role == "assistant" else "Lead"
            conv_parts.append(f"{label}: {text}")
        conv_text = "\n".join(conv_parts)

        user_msg = f"Call summary: {ai_summary}\n\nFull conversation:\n{conv_text}"

        # Get API key (campaign-level or global fallback)
        campaign_row = (
            sb.table("campaigns")
            .select("openai_api_key")
            .eq("id", body.campaign_id)
            .limit(1)
            .execute()
        )
        campaign_data = (campaign_row.data or [{}])[0]
        api_key = (campaign_data.get("openai_api_key") or "").strip() or settings.openai_api_key

        if not api_key:
            raise HTTPException(status_code=500, detail="No OpenAI API key configured")

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": AI_COACH_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 500,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                logger.error(
                    "ai_coach_http_error status=%s body=%s",
                    resp.status_code, resp.text[:500],
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"OpenAI API error: {resp.status_code}",
                )
            data = resp.json()

        # Extract text from response
        raw_text = ""
        choices = data.get("choices") or []
        if choices:
            raw_text = (choices[0].get("message", {}).get("content") or "").strip()

        # Parse JSON response
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            coaching = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            coaching = {
                "feedback": raw_text or "Unable to parse coaching feedback.",
                "strengths": [],
                "improvements": [],
                "tip_of_the_day": "Keep practicing your closing technique!",
            }

        return {"ok": True, "data": coaching}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "ai_coach_failed call=%s err=%s",
            body.call_record_id, str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=str(exc)[:200])
