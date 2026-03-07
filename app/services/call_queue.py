from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from ..deps import sb

logger = logging.getLogger("call_queue")


# ─── Default retry config ───────────────────────────────────────────────────
DEFAULT_RETRY_CONFIG = {
    "max_attempts_per_cycle": 3,
    "delay_between_attempts_seconds": [5, 10],
    "max_cycles": 7,
    "cycle_delay_hours": 24,
    "ring_timeout_seconds": 15,
    "amd_enabled": True,
}


def _get_campaign_retry_config(campaign_id: str) -> dict[str, Any]:
    """Fetch the call_retry_config for a campaign, falling back to defaults."""
    try:
        r = (
            sb.table("campaigns")
            .select("call_retry_config")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        cfg = ((r.data or [{}])[0] or {}).get("call_retry_config")
        if isinstance(cfg, dict) and cfg:
            return cfg
    except Exception as exc:
        logger.warning(
            "retry_config_fetch_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:200],
        )
    return dict(DEFAULT_RETRY_CONFIG)


# ─── Enqueue ─────────────────────────────────────────────────────────────────

def enqueue_call(
    *,
    campaign_id: str,
    lead_id: str,
    call_type: str = "initial_contact",
    priority: int = 0,
    scheduled_for: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Insert a new entry into call_queue. Returns the created row or None."""
    cfg = _get_campaign_retry_config(campaign_id)
    max_attempts = cfg.get("max_attempts_per_cycle", 3)
    max_cycles = cfg.get("max_cycles", 7)

    row: dict[str, Any] = {
        "campaign_id": campaign_id,
        "lead_id": lead_id,
        "call_type": call_type,
        "priority": priority,
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": max_attempts,
        "cycle_count": 0,
        "max_cycles": max_cycles,
    }
    if scheduled_for:
        row["scheduled_for"] = scheduled_for

    try:
        r = sb.table("call_queue").insert(row).execute()
        created = (r.data or [None])[0]
        logger.info(
            "call_enqueued campaign=%s lead=%s type=%s priority=%d",
            campaign_id,
            lead_id,
            call_type,
            priority,
        )
        return created
    except Exception as exc:
        logger.error(
            "enqueue_failed campaign=%s lead=%s err=%s",
            campaign_id,
            lead_id,
            str(exc)[:300],
        )
        return None


# ─── Assign (Spartan claims a call) ─────────────────────────────────────────

def assign_call(queue_id: str, user_id: str) -> Optional[dict[str, Any]]:
    """Mark a queued call as assigned to a spartan."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        r = (
            sb.table("call_queue")
            .update(
                {
                    "status": "assigned",
                    "assigned_to": user_id,
                    "assigned_at": now,
                    "updated_at": now,
                }
            )
            .eq("id", queue_id)
            .eq("status", "pending")
            .execute()
        )
        updated = (r.data or [None])[0]
        if updated:
            logger.info(
                "call_assigned queue=%s user=%s", queue_id, user_id
            )
        else:
            logger.warning(
                "call_assign_no_match queue=%s (already assigned or completed)",
                queue_id,
            )
        return updated
    except Exception as exc:
        logger.error(
            "assign_failed queue=%s err=%s", queue_id, str(exc)[:300]
        )
        return None


# ─── Complete ────────────────────────────────────────────────────────────────

def complete_call(
    *,
    queue_id: str,
    result: str,
    outcome: str = "",
    notes: str = "",
    tags: list[str] | None = None,
) -> Optional[dict[str, Any]]:
    """
    Mark a queued call as completed.

    If result is 'no_answer' and cycles remain, automatically re-queues
    for the next cycle (24h later).
    """
    now = datetime.now(timezone.utc).isoformat()

    try:
        # Fetch current queue entry
        qr = (
            sb.table("call_queue")
            .select("*")
            .eq("id", queue_id)
            .limit(1)
            .execute()
        )
        entry = (qr.data or [None])[0]
        if not entry:
            logger.warning("complete_call_not_found queue=%s", queue_id)
            return None

        # Update to completed
        update_data: dict[str, Any] = {
            "status": "completed",
            "result": result,
            "updated_at": now,
            "completed_at": now,
        }
        sb.table("call_queue").update(update_data).eq("id", queue_id).execute()

        logger.info(
            "call_completed queue=%s result=%s outcome=%s",
            queue_id,
            result,
            outcome,
        )

        # If no_answer and cycles remain → re-queue for next cycle
        if result == "no_answer":
            cycle_count = (entry.get("cycle_count") or 0) + 1
            max_cycles = entry.get("max_cycles") or 7
            if cycle_count < max_cycles:
                requeue_for_next_cycle(entry, cycle_count)

        return entry

    except Exception as exc:
        logger.error(
            "complete_failed queue=%s err=%s", queue_id, str(exc)[:300]
        )
        return None


# ─── Re-queue for next cycle ─────────────────────────────────────────────────

def requeue_for_next_cycle(
    original: dict[str, Any], cycle_count: int
) -> Optional[dict[str, Any]]:
    """
    Create a new call_queue entry scheduled for the next cycle.
    cycle_delay_hours (default 24) later.
    """
    campaign_id = original.get("campaign_id", "")
    cfg = _get_campaign_retry_config(campaign_id)
    delay_hours = cfg.get("cycle_delay_hours", 24)

    scheduled_for = (
        datetime.now(timezone.utc) + timedelta(hours=delay_hours)
    ).isoformat()

    row: dict[str, Any] = {
        "campaign_id": campaign_id,
        "lead_id": original.get("lead_id", ""),
        "call_type": original.get("call_type", "initial_contact"),
        "priority": original.get("priority", 0),
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": original.get("max_attempts", 3),
        "cycle_count": cycle_count,
        "max_cycles": original.get("max_cycles", 7),
        "scheduled_for": scheduled_for,
    }

    try:
        r = sb.table("call_queue").insert(row).execute()
        created = (r.data or [None])[0]
        logger.info(
            "call_requeued campaign=%s lead=%s cycle=%d/%d scheduled=%s",
            campaign_id,
            original.get("lead_id", ""),
            cycle_count,
            original.get("max_cycles", 7),
            scheduled_for,
        )
        return created
    except Exception as exc:
        logger.error(
            "requeue_failed campaign=%s lead=%s err=%s",
            campaign_id,
            original.get("lead_id", ""),
            str(exc)[:300],
        )
        return None


# ─── Get next call from queue ────────────────────────────────────────────────

def get_next_call(campaign_id: str) -> Optional[dict[str, Any]]:
    """
    Get the highest-priority pending call that is ready to be dialed.

    Ready means: status=pending AND (scheduled_for IS NULL OR scheduled_for <= now).
    Ordered by priority DESC, created_at ASC.
    """
    now = datetime.now(timezone.utc).isoformat()

    try:
        # Supabase doesn't support OR with IS NULL easily, so we do two queries
        # First: unscheduled pending calls
        r1 = (
            sb.table("call_queue")
            .select("*")
            .eq("campaign_id", campaign_id)
            .eq("status", "pending")
            .is_("scheduled_for", "null")
            .order("priority", desc=True)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )

        # Second: scheduled calls that are due
        r2 = (
            sb.table("call_queue")
            .select("*")
            .eq("campaign_id", campaign_id)
            .eq("status", "pending")
            .lte("scheduled_for", now)
            .order("priority", desc=True)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )

        candidates = (r1.data or []) + (r2.data or [])
        if not candidates:
            return None

        # Pick highest priority, then earliest created
        candidates.sort(
            key=lambda c: (-c.get("priority", 0), c.get("created_at", ""))
        )
        return candidates[0]

    except Exception as exc:
        logger.error(
            "get_next_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:300],
        )
        return None


# ─── Queue stats ─────────────────────────────────────────────────────────────

def get_queue_stats(campaign_id: str) -> dict[str, int]:
    """Return counts per status for a campaign's call queue."""
    stats: dict[str, int] = {
        "pending": 0,
        "assigned": 0,
        "completed": 0,
        "total": 0,
    }
    try:
        r = (
            sb.table("call_queue")
            .select("status")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        rows = r.data or []
        stats["total"] = len(rows)
        for row in rows:
            s = row.get("status", "pending")
            if s in stats:
                stats[s] += 1
    except Exception as exc:
        logger.error(
            "queue_stats_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:300],
        )
    return stats


# ─── Call Records ────────────────────────────────────────────────────────────

def create_call_record(
    *,
    campaign_id: str,
    lead_id: str,
    queue_id: Optional[str] = None,
    caller_type: str = "spartan",
    caller_id: Optional[str] = None,
    from_number: str = "",
    to_number: str = "",
    status: str = "initiated",
) -> Optional[dict[str, Any]]:
    """Create a new call record entry."""
    row: dict[str, Any] = {
        "campaign_id": campaign_id,
        "lead_id": lead_id,
        "caller_type": caller_type,
        "from_number": from_number,
        "to_number": to_number,
        "status": status,
    }
    if queue_id:
        row["queue_id"] = queue_id
    if caller_id:
        row["caller_id"] = caller_id

    try:
        r = sb.table("call_records").insert(row).execute()
        created = (r.data or [None])[0]
        logger.info(
            "call_record_created campaign=%s lead=%s type=%s",
            campaign_id,
            lead_id,
            caller_type,
        )
        return created
    except Exception as exc:
        logger.error(
            "call_record_create_failed campaign=%s lead=%s err=%s",
            campaign_id,
            lead_id,
            str(exc)[:300],
        )
        return None


def update_call_record(
    record_id: str, updates: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Update fields on an existing call record."""
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        r = (
            sb.table("call_records")
            .update(updates)
            .eq("id", record_id)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "call_record_update_failed id=%s err=%s",
            record_id,
            str(exc)[:300],
        )
        return None


# ─── Spartan Sessions ───────────────────────────────────────────────────────

def start_session(
    campaign_id: str, user_id: str
) -> Optional[dict[str, Any]]:
    """Start a new spartan calling session."""
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "campaign_id": campaign_id,
        "user_id": user_id,
        "status": "online",
        "calls_today": 0,
        "talk_time_today_seconds": 0,
        "started_at": now,
        "last_heartbeat_at": now,
    }
    try:
        r = sb.table("spartan_sessions").insert(row).execute()
        created = (r.data or [None])[0]
        logger.info(
            "session_started campaign=%s user=%s", campaign_id, user_id
        )
        return created
    except Exception as exc:
        logger.error(
            "session_start_failed campaign=%s user=%s err=%s",
            campaign_id,
            user_id,
            str(exc)[:300],
        )
        return None


def heartbeat_session(session_id: str) -> Optional[dict[str, Any]]:
    """Update the heartbeat timestamp for a spartan session."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        r = (
            sb.table("spartan_sessions")
            .update({"last_heartbeat_at": now})
            .eq("id", session_id)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:
        logger.error(
            "heartbeat_failed session=%s err=%s",
            session_id,
            str(exc)[:300],
        )
        return None


def end_session(session_id: str) -> Optional[dict[str, Any]]:
    """End a spartan session."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        r = (
            sb.table("spartan_sessions")
            .update(
                {
                    "status": "offline",
                    "ended_at": now,
                    "last_heartbeat_at": now,
                }
            )
            .eq("id", session_id)
            .execute()
        )
        updated = (r.data or [None])[0]
        logger.info("session_ended session=%s", session_id)
        return updated
    except Exception as exc:
        logger.error(
            "session_end_failed session=%s err=%s",
            session_id,
            str(exc)[:300],
        )
        return None


def get_active_sessions(campaign_id: str) -> list[dict[str, Any]]:
    """Get all online/busy spartan sessions for a campaign."""
    try:
        r = (
            sb.table("spartan_sessions")
            .select("*")
            .eq("campaign_id", campaign_id)
            .neq("status", "offline")
            .order("started_at", desc=True)
            .execute()
        )
        return r.data or []
    except Exception as exc:
        logger.error(
            "active_sessions_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:300],
        )
        return []
