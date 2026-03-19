from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..deps import sb

logger = logging.getLogger("commission_engine")


# ─── Attribute a sale to a spartan ──────────────────────────────────────────


async def attribute_sale(lead_id: str, campaign_id: str) -> dict | None:
    """Find which spartan last called this lead and create a commission record.
    Returns the commission dict or None if no spartan attribution found."""
    try:
        # 1. Check if commission already exists for this lead
        existing = (
            sb.table("commissions")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("campaign_id", campaign_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]  # Already attributed

        # 2. Find last spartan call for this lead
        call = (
            sb.table("call_records")
            .select("caller_id, id, created_at")
            .eq("lead_id", lead_id)
            .eq("campaign_id", campaign_id)
            .eq("caller_type", "spartan")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not call.data:
            return None  # No spartan call found — AI-only or organic sale

        agent_id = call.data[0]["caller_id"]
        call_record_id = call.data[0]["id"]

        # 3. Get commission config for this campaign
        config = (
            sb.table("commission_configs")
            .select("*")
            .eq("campaign_id", campaign_id)
            .execute()
        )

        # 4. Determine tier from lead
        lead = (
            sb.table("leads")
            .select("tier_interest, status")
            .eq("lead_id", lead_id)
            .limit(1)
            .execute()
        )
        tier = (lead.data[0] if lead.data else {}).get("tier_interest", "VIP")

        # Find matching config or use first one
        cfg = None
        for c in (config.data or []):
            if c["tier"].upper() == (tier or "VIP").upper():
                cfg = c
                break
        if not cfg and config.data:
            cfg = config.data[0]

        if not cfg:
            # No commission config — create record with $0
            commission_amount = 0
            commission_pct = 0
            sale_amount = 0
        else:
            # Check for volume-based escalation tiers
            agent_sales_count = 0
            try:
                sc = sb.table("commissions").select("id", count="exact").eq("campaign_id", campaign_id).eq("agent_id", agent_id).eq("tier", tier).limit(0).execute()
                agent_sales_count = sc.count or 0
            except Exception:
                pass

            # Look for escalation tier
            escalated_type = cfg.get("commission_type", "fixed")
            escalated_value = float(cfg.get("commission_value", 0))
            try:
                tiers_r = sb.table("commission_tiers").select("*").eq("config_id", cfg["id"]).order("min_sales", desc=True).execute()
                for t in (tiers_r.data or []):
                    if agent_sales_count >= t.get("min_sales", 0):
                        escalated_type = t.get("commission_type", escalated_type)
                        escalated_value = float(t.get("commission_value", escalated_value))
                        break
            except Exception:
                pass  # Fall back to base config

            # Use escalated values for commission calculation
            sale_amount = escalated_value if escalated_type == "fixed" else 0
            commission_pct = escalated_value if escalated_type == "percentage" else 0
            commission_amount = escalated_value  # For fixed, this IS the commission

        # 5. Insert commission
        record = {
            "campaign_id": campaign_id,
            "agent_id": agent_id,
            "lead_id": lead_id,
            "call_record_id": call_record_id,
            "tier": tier or "VIP",
            "sale_amount": sale_amount,
            "commission_pct": commission_pct,
            "commission_amount": commission_amount,
            "status": "pending",
        }
        r = sb.table("commissions").insert(record).execute()
        logger.info(
            "commission_attributed campaign=%s lead=%s agent=%s amount=%.2f",
            campaign_id, lead_id, agent_id, commission_amount,
        )
        return (r.data or [None])[0]

    except Exception as exc:
        logger.error(
            "attribute_sale_failed lead=%s campaign=%s err=%s",
            lead_id, campaign_id, str(exc)[:300],
        )
        return None


# ─── Agent earnings ─────────────────────────────────────────────────────────


async def get_agent_earnings(agent_id: str, campaign_id: str) -> dict:
    """Get earnings breakdown for an agent."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    try:
        all_commissions = (
            sb.table("commissions")
            .select("commission_amount, status, created_at")
            .eq("campaign_id", campaign_id)
            .eq("agent_id", agent_id)
            .execute()
        )

        earnings: dict[str, Any] = {
            "today": 0,
            "week": 0,
            "month": 0,
            "all_time": 0,
            "pending": 0,
            "paid": 0,
        }
        for c in (all_commissions.data or []):
            amt = float(c["commission_amount"] or 0)
            created = c["created_at"]
            earnings["all_time"] += amt
            if c["status"] == "pending":
                earnings["pending"] += amt
            elif c["status"] == "paid":
                earnings["paid"] += amt
            if created >= today_start:
                earnings["today"] += amt
            if created >= week_start:
                earnings["week"] += amt
            if created >= month_start:
                earnings["month"] += amt

        return earnings

    except Exception as exc:
        logger.error(
            "get_agent_earnings_failed agent=%s campaign=%s err=%s",
            agent_id, campaign_id, str(exc)[:300],
        )
        return {"today": 0, "week": 0, "month": 0, "all_time": 0, "pending": 0, "paid": 0}


# ─── Leaderboard ────────────────────────────────────────────────────────────


async def get_leaderboard(campaign_id: str) -> list:
    """Get agent leaderboard ranked by total commissions."""
    try:
        commissions = (
            sb.table("commissions")
            .select("agent_id, commission_amount, status")
            .eq("campaign_id", campaign_id)
            .execute()
        )

        # Aggregate by agent
        agents: dict[str, dict[str, Any]] = {}
        for c in (commissions.data or []):
            aid = c["agent_id"]
            if aid not in agents:
                agents[aid] = {"agent_id": aid, "total_earned": 0, "total_sales": 0, "pending": 0}
            agents[aid]["total_earned"] += float(c["commission_amount"] or 0)
            agents[aid]["total_sales"] += 1
            if c["status"] == "pending":
                agents[aid]["pending"] += float(c["commission_amount"] or 0)

        # Add call stats
        for aid in agents:
            try:
                calls = (
                    sb.table("call_records")
                    .select("id", count="exact")
                    .eq("campaign_id", campaign_id)
                    .eq("caller_id", aid)
                    .execute()
                )
                agents[aid]["total_calls"] = calls.count or 0
            except Exception:
                agents[aid]["total_calls"] = 0

        # Get display names from org_members
        for aid in agents:
            try:
                member = (
                    sb.table("org_members")
                    .select("display_name, email")
                    .eq("user_id", aid)
                    .limit(1)
                    .execute()
                )
                if member.data:
                    agents[aid]["display_name"] = (
                        member.data[0].get("display_name") or member.data[0].get("email", "Agent")
                    )
            except Exception:
                agents[aid]["display_name"] = "Agent"

        # Sort by total_earned desc
        ranked = sorted(agents.values(), key=lambda x: x["total_earned"], reverse=True)
        for i, a in enumerate(ranked):
            a["rank"] = i + 1
        return ranked

    except Exception as exc:
        logger.error(
            "get_leaderboard_failed campaign=%s err=%s",
            campaign_id, str(exc)[:300],
        )
        return []


# ─── Sync / backfill attributions ───────────────────────────────────────────


async def sync_all_attributions(campaign_id: str) -> dict:
    """Backfill commissions for all PAID leads without a commission record."""
    try:
        # Get all PAID leads
        paid = (
            sb.table("leads")
            .select("lead_id")
            .eq("campaign_id", campaign_id)
            .eq("payment_status", "PAID")
            .execute()
        )

        # Get existing commissions
        existing = (
            sb.table("commissions")
            .select("lead_id")
            .eq("campaign_id", campaign_id)
            .execute()
        )
        existing_leads = {c["lead_id"] for c in (existing.data or [])}

        created = 0
        skipped = 0
        no_agent = 0
        for lead in (paid.data or []):
            lid = lead["lead_id"]
            if lid in existing_leads:
                skipped += 1
                continue
            result = await attribute_sale(lid, campaign_id)
            if result:
                created += 1
            else:
                no_agent += 1

        summary = {
            "created": created,
            "skipped": skipped,
            "no_agent": no_agent,
            "total_paid": len(paid.data or []),
        }
        logger.info(
            "sync_attributions campaign=%s created=%d skipped=%d no_agent=%d",
            campaign_id, created, skipped, no_agent,
        )
        return summary

    except Exception as exc:
        logger.error(
            "sync_attributions_failed campaign=%s err=%s",
            campaign_id, str(exc)[:300],
        )
        return {"created": 0, "skipped": 0, "no_agent": 0, "total_paid": 0}
