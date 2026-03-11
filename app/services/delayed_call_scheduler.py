"""Delayed call scheduling — fires after a lead event.

Called from WhatsApp webhook after lead creation or status transition.
Uses asyncio.sleep to delay, then checks if intervention is still needed.

NOTE: asyncio tasks are lost on server restart. The cron job in
automation.py acts as a safety net for leads that miss their
delayed call (e.g. due to a deploy).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..deps import sb
from ..settings import settings
from .call_queue import (
    create_call_record,
    get_active_sessions,
    enqueue_call,
    update_call_record,
)
from .telnyx_calls import dial_outbound, _encode_client_state
from .number_pool import pick_number, detect_lead_country

logger = logging.getLogger("delayed_call_scheduler")

# Status progression — higher index = more progressed
_STATUS_RANK = {
    "NEW": 0,
    "GENERAL_CONFIRMED": 1,
    "VIP_INTERESTED": 2,
    "VIP_LINK_SENT": 3,
    "VIP_PAID": 4,
    "PAID": 4,
}


async def schedule_delayed_call(
    *,
    lead_id: str,
    campaign_id: str,
    delay_seconds: int,
    expected_status: str,
    purpose: str,
    priority: int = 0,
) -> None:
    """Wait delay_seconds, then check if the lead still needs a call.

    Conditions to trigger:
    1. Lead status hasn't progressed beyond expected_status
    2. Campaign has ai_calls_enabled = True
    3. No recent AI call to this lead in the last 24h
    4. If a spartan agent is active → enqueue for human
    5. Otherwise → trigger AI call directly
    """
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return

    try:
        # Re-fetch lead
        lr = (
            sb.table("leads")
            .select("*")
            .eq("lead_id", lead_id)
            .limit(1)
            .execute()
        )
        lead = (lr.data or [None])[0]
        if not lead:
            logger.info("delayed_call_skip_no_lead lead=%s", lead_id)
            return

        current_status = (lead.get("status") or "").upper().strip()

        # Check if status has progressed beyond expected
        expected_rank = _STATUS_RANK.get(expected_status.upper(), -1)
        current_rank = _STATUS_RANK.get(current_status, -1)

        if current_rank > expected_rank:
            logger.info(
                "delayed_call_skip_progressed lead=%s expected=%s current=%s",
                lead_id,
                expected_status,
                current_status,
            )
            return

        # For NEW leads: if they now have a name, they progressed (even if status didn't change)
        if expected_status.upper() == "NEW" and lead.get("name"):
            logger.info(
                "delayed_call_skip_has_name lead=%s name=%s",
                lead_id,
                lead.get("name", "")[:30],
            )
            return

        # Check if already paid
        if (lead.get("payment_status") or "").upper() == "PAID":
            logger.info("delayed_call_skip_paid lead=%s", lead_id)
            return

        # Do not contact
        if lead.get("do_not_contact"):
            return

        # Fetch campaign
        cr = (
            sb.table("campaigns")
            .select("*")
            .eq("id", campaign_id)
            .limit(1)
            .execute()
        )
        campaign = (cr.data or [None])[0]
        if not campaign:
            logger.info("delayed_call_skip_no_campaign campaign=%s", campaign_id)
            return

        if not campaign.get("ai_calls_enabled"):
            logger.info("delayed_call_skip_ai_disabled campaign=%s", campaign_id)
            return

        # Check for active agents — if available, enqueue for human
        active_agents = get_active_sessions(campaign_id)
        if active_agents:
            enqueue_call(
                campaign_id=campaign_id,
                lead_id=lead_id,
                call_type="delayed_auto",
                priority=priority,
            )
            logger.info(
                "delayed_call_enqueued_human lead=%s agents=%d",
                lead_id,
                len(active_agents),
            )
            return

        # Anti-spam: check for recent AI call in last 24h
        now = datetime.now(timezone.utc)
        try:
            recent_r = (
                sb.table("call_records")
                .select("id")
                .eq("campaign_id", campaign_id)
                .eq("lead_id", lead_id)
                .eq("caller_type", "ai")
                .gte("created_at", (now - timedelta(hours=24)).isoformat())
                .limit(1)
                .execute()
            )
            if recent_r.data:
                logger.info("delayed_call_skip_recent_ai lead=%s", lead_id)
                return
        except Exception:
            pass

        # Telnyx credentials
        telnyx_api_key = (campaign.get("telnyx_api_key") or "").strip()
        connection_id = (
            (campaign.get("telnyx_webrtc_credential_id") or "").strip()
            or (campaign.get("telnyx_sip_connection_id") or "").strip()
        )

        if not telnyx_api_key or not connection_id:
            logger.warning(
                "delayed_call_skip_no_telnyx campaign=%s", campaign_id
            )
            return

        # Normalize phone number
        phone = (lead.get("phone") or lead.get("whatsapp") or "").strip()
        if phone.startswith("whatsapp:"):
            phone = phone[len("whatsapp:"):]
        # MX WhatsApp numbers use +521 but Telnyx voice needs +52
        if phone.startswith("+521") and len(phone) == 14:
            phone = "+52" + phone[4:]
        if not phone:
            logger.info("delayed_call_skip_no_phone lead=%s", lead_id)
            return

        # Pick best number from pool (falls back to campaign.telnyx_from_number)
        lead_country = detect_lead_country(phone)
        from_number = await pick_number(campaign_id, campaign, country=lead_country)
        if not from_number:
            logger.warning("delayed_call_skip_no_from_number campaign=%s lead=%s", campaign_id, lead_id)
            return

        # Trigger AI call
        client_state = _encode_client_state(
            {
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "caller_type": "ai",
                "purpose": purpose,
            }
        )

        webhook_url = (
            f"{settings.public_base_url}/v1/calls/telnyx/webhooks/{campaign_id}"
            if settings.public_base_url
            else ""
        )

        dial_result = await dial_outbound(
            to_number=phone,
            from_number=from_number,
            connection_id=connection_id,
            telnyx_api_key=telnyx_api_key,
            webhook_url=webhook_url,
            client_state=client_state,
            amd="",
        )

        call_control_id = dial_result.get("call_control_id", "")

        record = create_call_record(
            campaign_id=campaign_id,
            lead_id=lead_id,
            caller_type="ai",
            from_number=from_number,
            to_number=phone,
            status="initiated",
            notes=f"purpose:{purpose}|trigger:delayed_auto",
            purpose=purpose,
        )
        if record and call_control_id:
            update_call_record(
                record["id"], {"telnyx_call_control_id": call_control_id}
            )

        logger.info(
            "delayed_ai_call_triggered lead=%s purpose=%s delay=%ds phone=%s",
            lead_id,
            purpose,
            delay_seconds,
            phone,
        )

    except Exception as exc:
        logger.error(
            "delayed_call_failed lead=%s err=%s",
            lead_id,
            str(exc)[:300],
        )
