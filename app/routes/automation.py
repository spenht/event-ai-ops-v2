"""Automated follow-up engine.

Called periodically by GitHub Actions (every 5 min).
The backend decides whether each lead deserves a follow-up right now.

Rules
-----
1. followup_15m  — 15 min after last outbound with no reply
2. followup_1h   — 1 h after last outbound with no reply (only if 15m already sent)
3. followup_daily — once per calendar day, value-driven content

Anti-spam
---------
- Each follow-up type is recorded in touchpoints with a unique key per lead + type + date.
- If the lead replied after our last outbound, skip (they're engaged).
- Max 3 follow-ups per lead per day.
- Never contact do_not_contact leads or PAID leads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..deps import sb
from ..settings import settings
from ..services.twilio_whatsapp import send_whatsapp
from ..routes.broadcasts import execute_campaign
from ..services.delayed_call_scheduler import schedule_delayed_call

logger = logging.getLogger("automation")

router = APIRouter(prefix="/v1/automation", tags=["automation"])

# ---------------------------------------------------------------------------
# Follow-up message templates
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, dict[str, list[str]]] = {
    "followup_15m": {
        "GENERAL_CONFIRMED": [
            "{name} 👋 ya tienes tu lugar en *General*.\n\n"
            "¿Sabías que el *VIP* incluye primera fila, regalos y mastermind? Te cuento más si quieres 😊",
        ],
        "VIP_INTERESTED": [
            "Hey {name} 😊 vi que te interesó el *VIP*.\n\n"
            "¿Alguna duda? Te ayudo rapidísimo.",
        ],
        "VIP_LINK_SENT": [
            "{name}, ¿pudiste ver el link de pago? 🤔\n\n"
            "Si tuviste algún problema dime y te ayudo ahorita.",
        ],
    },
    "followup_1h": {
        "GENERAL_CONFIRMED": [
            "{name} 🙌 el *VIP* tiene cupo limitado.\n\n"
            "Primera fila + mastermind + libro de regalo.\n\n"
            "¿Te mando un video corto de lo que incluye? 🎥",
        ],
        "VIP_INTERESTED": [
            "{name}, los lugares *VIP* se están llenando rápido.\n\n"
            "¿Te mando el link de pago? 🔥",
        ],
        "VIP_LINK_SENT": [
            "{name}, te mandé el link hace rato.\n\n"
            "¿Necesitas que te lo reenvíe? A veces WhatsApp los esconde 😅",
        ],
    },
    "followup_daily": {
        "GENERAL_CONFIRMED": [
            "{name} ☀️ alguien que fue VIP la vez pasada nos dijo:\n\n"
            "💬 *\"Fue la mejor inversión del año.\"*\n\n"
            "¿Te cuento qué incluye el VIP?",
        ],
        "VIP_INTERESTED": [
            "{name} 👋 el evento es pronto y quedan muy pocos VIP.\n\n"
            "¿Te mando el link directo?",
        ],
        "VIP_LINK_SENT": [
            "{name} 🔥 los boletos VIP se están agotando súper rápido y quedan muy poquitos.\n\n"
            "Tu link sigue activo. ¿Lo pagas hoy?",
        ],
    },
}

DEFAULT_MSG = "Hola {name} 👋 ¿Cómo vas? Aquí seguimos por si tienes alguna duda del evento 😊"

# ---------------------------------------------------------------------------
# Eligible statuses (leads we want to follow up)
# ---------------------------------------------------------------------------
ELIGIBLE_STATUSES = ["GENERAL_CONFIRMED", "VIP_INTERESTED", "VIP_LINK_SENT"]

MAX_FOLLOWUPS_PER_DAY = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_cron_token(request: Request) -> None:
    """Validate X-Cron-Token header if CRON_TOKEN is configured."""
    if not settings.cron_token:
        return  # No token configured = open (dev mode)
    token = (request.headers.get("x-cron-token") or "").strip()
    if token != settings.cron_token:
        raise HTTPException(status_code=403, detail="invalid cron token")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_key(followup_type: str) -> str:
    """Unique key for dedup: type + date."""
    return f"{followup_type}_{_utcnow().strftime('%Y-%m-%d')}"


def _get_last_touchpoint(lead_id: str, event_types: list[str]) -> dict[str, Any] | None:
    """Get the most recent touchpoint of given types for a lead."""
    try:
        r = (
            sb.table("touchpoints")
            .select("event_type,payload,created_at")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .in_("event_type", event_types)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception:
        return None


def _count_today_followups(lead_id: str) -> int:
    """Count how many follow-ups we already sent today."""
    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .in_("event_type", ["followup_15m", "followup_1h", "followup_daily"])
            .gte("created_at", today_start)
            .execute()
        )
        return len(r.data or [])
    except Exception:
        return 0


def _followup_already_sent(lead_id: str, followup_type: str) -> bool:
    """Check if this specific follow-up type was already sent today."""
    key = _today_key(followup_type)
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", followup_type)
            .contains("payload", {"key": key})
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


def _lead_replied_after(lead_id: str, after_ts: str) -> bool:
    """Did the lead send any inbound message after the given timestamp?"""
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", "inbound")
            .gt("created_at", after_ts)
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


def _pick_message(followup_type: str, status: str, name: str) -> str:
    """Pick a message template for this follow-up type and lead status."""
    templates = TEMPLATES.get(followup_type, {})
    msgs = templates.get(status, [DEFAULT_MSG])
    msg = msgs[0]  # For now pick first; later can rotate/randomize
    return msg.format(name=name or "")


def _decide_followup(lead: dict[str, Any], now: datetime) -> str | None:
    """Decide which follow-up to send (if any).

    Returns followup type string or None.
    Priority: 15m > 1h > daily
    """
    lead_id = lead["lead_id"]
    status = (lead.get("status") or "").strip()

    # Rate limit
    if _count_today_followups(lead_id) >= MAX_FOLLOWUPS_PER_DAY:
        return None

    # Get last outbound (AI reply or follow-up)
    last_out = _get_last_touchpoint(lead_id, ["outbound_ai", "followup_15m", "followup_1h", "followup_daily"])
    if not last_out:
        # Never contacted — send 15m follow-up immediately
        if not _followup_already_sent(lead_id, "followup_15m"):
            return "followup_15m"
        return None

    last_out_ts = last_out.get("created_at", "")
    try:
        last_out_dt = datetime.fromisoformat(last_out_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    # If lead replied after our last outbound, they're engaged — don't interrupt
    if _lead_replied_after(lead_id, last_out_ts):
        return None

    elapsed = now - last_out_dt
    minutes_elapsed = elapsed.total_seconds() / 60

    # 15-minute follow-up
    if minutes_elapsed >= 15 and not _followup_already_sent(lead_id, "followup_15m"):
        return "followup_15m"

    # 1-hour follow-up (only if 15m was already sent)
    if minutes_elapsed >= 60 and _followup_already_sent(lead_id, "followup_15m") and not _followup_already_sent(lead_id, "followup_1h"):
        return "followup_1h"

    # Daily follow-up (only if 1h was already sent)
    if minutes_elapsed >= 1440 and _followup_already_sent(lead_id, "followup_1h") and not _followup_already_sent(lead_id, "followup_daily"):
        return "followup_daily"

    return None


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/followups")
async def run_followups(request: Request):
    """Process follow-ups for eligible leads.

    Called by GitHub Actions every 5 minutes.
    """
    _validate_cron_token(request)

    now = _utcnow()

    # Query eligible leads
    try:
        r = (
            sb.table("leads")
            .select("*")
            .in_("status", ELIGIBLE_STATUSES)
            .neq("payment_status", "PAID")
            .eq("do_not_contact", False)
            .execute()
        )
        leads = r.data or []
    except Exception as e:
        logger.exception("followup_query_failed")
        raise HTTPException(status_code=500, detail=f"query failed: {str(e)[:200]}")

    # Pre-load campaign Twilio credentials for per-campaign sending
    campaign_creds_cache: dict[str, dict[str, str]] = {}

    def _get_campaign_creds(campaign_id: str) -> dict[str, str]:
        if campaign_id in campaign_creds_cache:
            return campaign_creds_cache[campaign_id]
        creds: dict[str, str] = {}
        if campaign_id:
            try:
                cr = sb.table("campaigns").select(
                    "twilio_account_sid,twilio_auth_token,twilio_whatsapp_from"
                ).eq("id", campaign_id).single().execute()
                c = cr.data or {}
                if c.get("twilio_account_sid"):
                    creds["account_sid"] = c["twilio_account_sid"]
                if c.get("twilio_auth_token"):
                    creds["auth_token"] = c["twilio_auth_token"]
                if c.get("twilio_whatsapp_from"):
                    creds["whatsapp_from"] = c["twilio_whatsapp_from"]
            except Exception:
                pass
        campaign_creds_cache[campaign_id] = creds
        return creds

    processed = 0
    sent = 0
    errors = 0

    for lead in leads:
        lead_id = lead.get("lead_id", "")
        name = (lead.get("name") or "").strip() or "amigo/a"
        status = (lead.get("status") or "").strip()
        wa = (lead.get("whatsapp") or "").strip()

        if not wa:
            continue

        processed += 1

        followup_type = _decide_followup(lead, now)
        if not followup_type:
            continue

        msg = _pick_message(followup_type, status, name)
        wa_kwargs = _get_campaign_creds(lead.get("campaign_id") or "")

        try:
            sid = await send_whatsapp(to_e164=wa, body=msg, **wa_kwargs)
            logger.info(
                "followup_sent type=%s lead=%s status=%s sid=%s",
                followup_type, lead_id, status, sid,
            )
            sent += 1
        except Exception as e:
            logger.error("followup_send_failed lead=%s err=%s", lead_id, str(e)[:200])
            errors += 1
            continue

        # Record the follow-up in touchpoints
        try:
            sb.table("touchpoints").insert({
                "lead_id": lead_id,
                "channel": "whatsapp",
                "event_type": followup_type,
                "payload": {
                    "key": _today_key(followup_type),
                    "body": msg,
                    "status_at_send": status,
                    "sid": sid,
                },
            }).execute()
        except Exception:
            pass

        # Update last_contact_at on the lead
        try:
            sb.table("leads").update({
                "last_contact_at": now.isoformat(),
            }).eq("lead_id", lead_id).execute()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Process scheduled messages (e.g. calendar reminders 10 min later)
    # ------------------------------------------------------------------
    sched_sent = 0
    try:
        sched_r = (
            sb.table("touchpoints")
            .select("*")
            .eq("channel", "whatsapp")
            .eq("event_type", "scheduled_message")
            .execute()
        )
        for tp in (sched_r.data or []):
            payload = tp.get("payload") or {}
            if payload.get("status") != "pending":
                continue
            send_after = payload.get("send_after", "")
            try:
                send_after_dt = datetime.fromisoformat(send_after.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if now < send_after_dt:
                continue
            wa = (payload.get("wa") or "").strip()
            body = (payload.get("body") or "").strip()
            if not wa or not body:
                continue
            # Resolve per-campaign Twilio creds from lead
            sched_wa_kwargs: dict[str, str] = {}
            try:
                lr = sb.table("leads").select("campaign_id").eq("lead_id", tp["lead_id"]).single().execute()
                sched_wa_kwargs = _get_campaign_creds((lr.data or {}).get("campaign_id") or "")
            except Exception:
                pass
            try:
                await send_whatsapp(to_e164=wa, body=body, **sched_wa_kwargs)
                payload["status"] = "sent"
                sb.table("touchpoints").update({"payload": payload}).eq("id", tp["id"]).execute()
                sched_sent += 1
                logger.info("scheduled_msg_sent tp_id=%s type=%s", tp.get("id"), payload.get("type"))
            except Exception as e:
                logger.error("scheduled_msg_failed tp_id=%s err=%s", tp.get("id"), str(e)[:200])
    except Exception:
        logger.exception("scheduled_msg_query_failed")

    # ------------------------------------------------------------------
    # Process due broadcast campaigns
    # ------------------------------------------------------------------
    broadcasts_executed = 0
    try:
        bc_r = (
            sb.table("broadcasts")
            .select("id,campaign_name,template_name,scheduled_at,status")
            .eq("status", "scheduled")
            .lte("scheduled_at", now.isoformat())
            .execute()
        )
        due_campaigns = bc_r.data or []
        for bc in due_campaigns:
            bc_id = bc.get("id", "")
            bc_name = bc.get("campaign_name", "")
            try:
                stats = await execute_campaign(bc_id)
                broadcasts_executed += 1
                logger.info(
                    "broadcast_campaign_executed id=%s name=%s sent=%s failed=%s",
                    bc_id, bc_name, stats.get("sent", 0), stats.get("failed", 0),
                )
            except Exception as e:
                logger.error(
                    "broadcast_campaign_failed id=%s name=%s err=%s",
                    bc_id, bc_name, str(e)[:300],
                )
    except Exception:
        logger.exception("broadcast_campaign_query_failed")

    # ------------------------------------------------------------------
    # AI Auto-Call recovery — catch leads that missed their delayed call
    # (e.g. due to server restart losing asyncio tasks)
    # ------------------------------------------------------------------
    auto_calls_scheduled = 0
    try:
        # Find campaigns with AI calls enabled
        ai_campaigns_r = (
            sb.table("campaigns")
            .select("id, ai_calls_enabled, ai_call_delay_minutes")
            .eq("ai_calls_enabled", True)
            .execute()
        )
        ai_campaigns = ai_campaigns_r.data or []

        for camp in ai_campaigns:
            camp_id = camp["id"]
            delay_min = camp.get("ai_call_delay_minutes") or 10

            # Find NEW leads older than delay_minutes that haven't been called
            # Two queries: one for NULL last_contact_at (imported leads), one for stale contacts
            cutoff = (now - timedelta(minutes=int(delay_min))).isoformat()
            uncalled_leads = []
            try:
                # Query 1: Leads never contacted (imported) — use created_at as fallback
                null_r = (
                    sb.table("leads")
                    .select("lead_id, status, phone, whatsapp")
                    .eq("campaign_id", camp_id)
                    .eq("status", "NEW")
                    .neq("do_not_contact", True)
                    .is_("last_contact_at", "null")
                    .limit(20)
                    .execute()
                )
                uncalled_leads.extend(null_r.data or [])
            except Exception:
                pass
            try:
                # Query 2: Leads contacted but past delay threshold
                stale_r = (
                    sb.table("leads")
                    .select("lead_id, status, phone, whatsapp")
                    .eq("campaign_id", camp_id)
                    .eq("status", "NEW")
                    .neq("do_not_contact", True)
                    .lte("last_contact_at", cutoff)
                    .limit(20)
                    .execute()
                )
                uncalled_leads.extend(stale_r.data or [])
            except Exception:
                pass
            # Dedup by lead_id and cap at 20
            seen_ids: set[str] = set()
            deduped: list[dict] = []
            for ul in uncalled_leads:
                if ul["lead_id"] not in seen_ids:
                    seen_ids.add(ul["lead_id"])
                    deduped.append(ul)
                if len(deduped) >= 20:
                    break
            uncalled_leads = deduped

            for ul in uncalled_leads:
                ul_id = ul["lead_id"]
                phone = (ul.get("phone") or ul.get("whatsapp") or "").strip()
                if not phone:
                    continue

                # Check if there's already a pending/assigned call in queue
                try:
                    existing_r = (
                        sb.table("call_queue")
                        .select("id")
                        .eq("campaign_id", camp_id)
                        .eq("lead_id", ul_id)
                        .in_("status", ["pending", "assigned"])
                        .limit(1)
                        .execute()
                    )
                    if existing_r.data:
                        continue  # Already in queue
                except Exception:
                    continue

                # Check if AI already called in last 24h
                try:
                    recent_call_r = (
                        sb.table("call_records")
                        .select("id")
                        .eq("campaign_id", camp_id)
                        .eq("lead_id", ul_id)
                        .gte("created_at", (now - timedelta(hours=24)).isoformat())
                        .limit(1)
                        .execute()
                    )
                    if recent_call_r.data:
                        continue  # Already called recently
                except Exception:
                    continue

                # Schedule immediate call (0 delay — they already waited)
                try:
                    await schedule_delayed_call(
                        lead_id=ul_id,
                        campaign_id=camp_id,
                        delay_seconds=0,
                        expected_status="NEW",
                        purpose="confirm_attendance",
                        priority=1,
                    )
                    auto_calls_scheduled += 1
                    logger.info("auto_call_recovery lead=%s campaign=%s", ul_id, camp_id)
                except Exception as exc:
                    logger.warning("auto_call_recovery_failed lead=%s err=%s", ul_id, str(exc)[:200])

        if auto_calls_scheduled:
            logger.info("auto_call_recovery_total=%d", auto_calls_scheduled)
    except Exception:
        logger.exception("auto_call_recovery_query_failed")

    # ------------------------------------------------------------------
    # Sync sales leads to Google Sheets (Spartans list)
    # ------------------------------------------------------------------
    gsheet_synced = 0
    try:
        from ..services.google_sheets import sync_sales_leads_sheet
        gsheet_result = await sync_sales_leads_sheet()
        gsheet_synced = gsheet_result.get("synced", 0)
        if gsheet_synced:
            logger.info("gsheet_sales_sync synced=%d", gsheet_synced)
    except Exception:
        logger.exception("gsheet_sales_sync_failed")

    logger.info(
        "followup_run processed=%d sent=%d errors=%d scheduled=%d broadcasts=%d gsheet=%d auto_calls=%d",
        processed, sent, errors, sched_sent, broadcasts_executed, gsheet_synced, auto_calls_scheduled,
    )
    return {
        "processed": processed,
        "sent": sent,
        "errors": errors,
        "scheduled_sent": sched_sent,
        "broadcasts_executed": broadcasts_executed,
        "gsheet_sales_synced": gsheet_synced,
        "auto_calls_scheduled": auto_calls_scheduled,
    }
