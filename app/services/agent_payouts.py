"""Agent Payout Service — Stripe Connect onboarding + automated commission transfers.

Flow:
1. Agent onboards via Stripe Express (connect bank account)
2. Daily cron groups pending commissions by agent + source Stripe account
3. Creates Stripe Transfers from source account → agent's Connect account
4. Marks commissions as paid, records payout batch for audit
"""
from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import stripe

from ..deps import sb

logger = logging.getLogger("agent_payouts")

# Stripe key map — same as agent_terminal but we need it here too
_STRIPE_KEY_MAP = {
    "lba": "STRIPE_KEY_LBA",
    "uvul": "STRIPE_KEY_UVUL",
    "oll": "STRIPE_KEY_OLL",
    "2clicks": "STRIPE_KEY_2CLICKS",
}

PLATFORM_KEY_ENV = "STRIPE_PLATFORM_SECRET_KEY"


def _stripe_client(account_key: str = "") -> stripe.StripeClient:
    """Return a Stripe client for the given account key (lba, uvul, etc.)."""
    if account_key:
        env_var = _STRIPE_KEY_MAP.get(account_key, "")
        key = os.getenv(env_var, "") if env_var else ""
    else:
        key = os.getenv(PLATFORM_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"Missing Stripe key for account: {account_key or 'platform'}")
    return stripe.StripeClient(key)


# ── Agent Stripe Connect Onboarding ─────────────────────────────


def create_agent_connect_account(
    *,
    user_id: str,
    email: str,
    name: str = "",
    country: str = "US",
) -> dict[str, Any]:
    """Create a Stripe Express connected account for an agent.

    Returns {stripe_account_id, onboarding_url}.
    """
    client = _stripe_client()  # platform key

    # Check if agent already has a profile
    existing = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )

    if existing.data and existing.data[0].get("stripe_connect_account_id"):
        # Already has an account — generate fresh onboarding link
        acct_id = existing.data[0]["stripe_connect_account_id"]
        link = client.account_links.create(
            params={
                "account": acct_id,
                "type": "account_onboarding",
                "return_url": "https://dashboard-jade-one-94.vercel.app/dashboard/agent/terminal?stripe=complete",
                "refresh_url": "https://dashboard-jade-one-94.vercel.app/dashboard/agent/terminal?stripe=refresh",
            }
        )
        return {"stripe_account_id": acct_id, "onboarding_url": link.url}

    # Create new Express account
    account = client.accounts.create(
        params={
            "type": "express",
            "country": country,
            "email": email,
            "capabilities": {
                "transfers": {"requested": True},
            },
            "metadata": {
                "user_id": user_id,
                "platform": "event-ai-ops",
                "type": "agent",
            },
            "business_type": "individual",
        }
    )

    # Generate onboarding link
    link = client.account_links.create(
        params={
            "account": account.id,
            "type": "account_onboarding",
            "return_url": "https://dashboard-jade-one-94.vercel.app/dashboard/agent/terminal?stripe=complete",
            "refresh_url": "https://dashboard-jade-one-94.vercel.app/dashboard/agent/terminal?stripe=refresh",
        }
    )

    # Upsert agent_payout_profiles
    profile_data = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "stripe_connect_account_id": account.id,
        "stripe_connect_status": "onboarding",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing.data:
        sb.table("agent_payout_profiles").update(profile_data).eq("user_id", user_id).execute()
    else:
        sb.table("agent_payout_profiles").insert(profile_data).execute()

    return {"stripe_account_id": account.id, "onboarding_url": link.url}


def get_agent_connect_status(user_id: str) -> dict[str, Any]:
    """Check if agent has completed Stripe Connect onboarding."""
    profile = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )

    if not profile.data:
        return {"status": "not_connected", "payouts_enabled": False}

    p = profile.data[0]
    acct_id = p.get("stripe_connect_account_id")
    if not acct_id:
        return {"status": "not_connected", "payouts_enabled": False}

    # Check live status from Stripe
    try:
        client = _stripe_client()
        acct = client.accounts.retrieve(acct_id)
        payouts_enabled = getattr(acct, "payouts_enabled", False)
        details_submitted = getattr(acct, "details_submitted", False)

        if payouts_enabled:
            status = "active"
        elif details_submitted:
            status = "pending_verification"
        else:
            status = "onboarding"

        # Update local status
        if status != p.get("stripe_connect_status"):
            update = {"stripe_connect_status": status}
            if status == "active" and not p.get("onboarded_at"):
                update["onboarded_at"] = datetime.now(timezone.utc).isoformat()
            sb.table("agent_payout_profiles").update(update).eq("user_id", user_id).execute()

        return {
            "status": status,
            "stripe_account_id": acct_id,
            "payouts_enabled": payouts_enabled,
            "details_submitted": details_submitted,
            "payout_frequency": p.get("payout_frequency", "weekly"),
        }
    except Exception as e:
        logger.error("Failed to check Connect status for %s: %s", user_id, e)
        return {
            "status": p.get("stripe_connect_status", "unknown"),
            "stripe_account_id": acct_id,
            "payouts_enabled": False,
            "payout_frequency": p.get("payout_frequency", "weekly"),
        }


# ── Payout Calculation & Execution ──────────────────────────────


def _should_pay_today(frequency: str) -> bool:
    """Check if the given frequency means we should pay today."""
    today = datetime.now(timezone.utc)
    day_of_week = today.weekday()  # 0=Monday
    day_of_month = today.day

    if frequency == "daily":
        return True
    elif frequency == "weekly":
        return day_of_week == 0  # Monday
    elif frequency == "biweekly":
        return day_of_month in (1, 15)
    elif frequency == "monthly":
        return day_of_month == 1
    elif frequency == "manual":
        return False
    return False


def calculate_pending_payouts(*, force: bool = False) -> list[dict]:
    """Calculate pending commission payouts grouped by agent + source account.

    Returns list of payout batches ready to execute.
    """
    # Get all pending commissions
    pending = (
        sb.table("commissions")
        .select("*")
        .eq("status", "pending")
        .execute()
    )

    if not pending.data:
        return []

    # Get all agent payout profiles (only active ones)
    profiles = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("stripe_connect_status", "active")
        .execute()
    )
    profile_map = {p["user_id"]: p for p in (profiles.data or [])}

    # Group commissions by agent_id + source stripe account
    batches: dict[str, dict] = {}
    for comm in pending.data:
        agent_id = comm.get("agent_id", "")
        if not agent_id:
            continue

        # Check if agent has active Connect account
        profile = profile_map.get(agent_id)
        if not profile:
            continue

        # Check payout frequency
        freq = profile.get("payout_frequency", "weekly")
        if not force and not _should_pay_today(freq):
            continue

        # Determine source Stripe account from commission metadata or notes
        source_account = "lba"  # default
        metadata = comm.get("metadata") or {}
        if isinstance(metadata, dict):
            gw_key = metadata.get("gateway_key", "")
            if gw_key in _STRIPE_KEY_MAP:
                source_account = gw_key

        batch_key = f"{agent_id}:{source_account}"
        if batch_key not in batches:
            batches[batch_key] = {
                "agent_id": agent_id,
                "stripe_connect_account_id": profile["stripe_connect_account_id"],
                "source_stripe_account": source_account,
                "commissions": [],
                "total_amount": 0.0,
                "currency": "USD",
            }

        batches[batch_key]["commissions"].append(comm)
        batches[batch_key]["total_amount"] += float(comm.get("commission_amount") or 0)

    return list(batches.values())


def execute_payout(batch: dict) -> dict[str, Any]:
    """Execute a single payout batch — transfer funds to agent's Connect account."""
    agent_id = batch["agent_id"]
    connect_id = batch["stripe_connect_account_id"]
    source_account = batch["source_stripe_account"]
    total_cents = int(round(batch["total_amount"] * 100))
    commission_ids = [c["id"] for c in batch["commissions"]]

    if total_cents <= 0:
        return {"ok": False, "error": "Amount must be positive"}

    try:
        # Use the source Stripe account to create the transfer
        client = _stripe_client(source_account)

        transfer = client.transfers.create(
            params={
                "amount": total_cents,
                "currency": batch.get("currency", "usd").lower(),
                "destination": connect_id,
                "description": f"Commission payout - {len(commission_ids)} sales",
                "metadata": {
                    "agent_id": agent_id,
                    "commission_count": str(len(commission_ids)),
                    "source": "agent_terminal_payout",
                    "batch_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                },
            }
        )

        transfer_id = transfer.id
        now_iso = datetime.now(timezone.utc).isoformat()

        # Record payout batch
        sb.table("payout_batches").insert({
            "agent_id": agent_id,
            "stripe_transfer_id": transfer_id,
            "source_stripe_account": source_account,
            "amount": batch["total_amount"],
            "currency": batch.get("currency", "USD"),
            "commission_count": len(commission_ids),
            "commission_ids": commission_ids,
            "status": "completed",
        }).execute()

        # Mark commissions as paid
        for cid in commission_ids:
            sb.table("commissions").update({
                "status": "paid",
                "paid_at": now_iso,
                "payout_ref": transfer_id,
            }).eq("id", cid).execute()

        logger.info(
            "Payout completed: agent=%s amount=$%.2f transfer=%s commissions=%d",
            agent_id, batch["total_amount"], transfer_id, len(commission_ids),
        )

        return {
            "ok": True,
            "transfer_id": transfer_id,
            "amount": batch["total_amount"],
            "commission_count": len(commission_ids),
        }

    except Exception as e:
        logger.error("Payout FAILED: agent=%s error=%s", agent_id, e)

        # Record failed batch
        sb.table("payout_batches").insert({
            "agent_id": agent_id,
            "stripe_transfer_id": f"FAILED_{uuid.uuid4().hex[:8]}",
            "source_stripe_account": source_account,
            "amount": batch["total_amount"],
            "currency": batch.get("currency", "USD"),
            "commission_count": len(commission_ids),
            "commission_ids": commission_ids,
            "status": "failed",
            "error_message": str(e)[:500],
        }).execute()

        return {"ok": False, "error": str(e)}


def _calculate_collaborator_payouts(*, force: bool = False) -> list[dict]:
    """Calculate fixed-amount payouts for collaborators (non-sales team).

    Returns list of payout dicts ready to execute via execute_collaborator_payout().
    """
    profiles = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("profile_type", "collaborator")
        .eq("stripe_connect_status", "active")
        .gt("fixed_amount", 0)
        .execute()
    )

    if not profiles.data:
        return []

    payouts = []
    for p in profiles.data:
        freq = p.get("payout_frequency", "biweekly")
        if not force and not _should_pay_today(freq):
            continue

        payouts.append({
            "user_id": p["user_id"],
            "name": p.get("name", ""),
            "email": p.get("email", ""),
            "stripe_connect_account_id": p["stripe_connect_account_id"],
            "source_stripe_account": p.get("source_stripe_account", "lba"),
            "fixed_amount": float(p["fixed_amount"]),
            "currency": p.get("currency", "USD"),
            "payout_frequency": freq,
        })

    return payouts


def _execute_collaborator_payout(collab: dict) -> dict[str, Any]:
    """Execute a single fixed-amount payout to a collaborator's Connect account."""
    user_id = collab["user_id"]
    connect_id = collab["stripe_connect_account_id"]
    source_account = collab["source_stripe_account"]
    amount = collab["fixed_amount"]
    total_cents = int(round(amount * 100))
    currency = collab.get("currency", "USD")

    if total_cents <= 0:
        return {"ok": False, "error": "Amount must be positive"}

    try:
        client = _stripe_client(source_account)

        transfer = client.transfers.create(
            params={
                "amount": total_cents,
                "currency": currency.lower(),
                "destination": connect_id,
                "description": f"Fixed {collab['payout_frequency']} payout - {collab.get('name', user_id)}",
                "metadata": {
                    "user_id": user_id,
                    "payout_type": "collaborator_fixed",
                    "source": "collaborator_payout",
                    "batch_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                },
            }
        )

        transfer_id = transfer.id

        # Record payout batch
        sb.table("payout_batches").insert({
            "agent_id": user_id,
            "stripe_transfer_id": transfer_id,
            "source_stripe_account": source_account,
            "amount": amount,
            "currency": currency,
            "commission_count": 0,
            "commission_ids": [],
            "status": "completed",
            "notes": f"Fixed {collab['payout_frequency']} payout",
        }).execute()

        logger.info(
            "Collaborator payout completed: user=%s amount=$%.2f transfer=%s",
            user_id, amount, transfer_id,
        )

        return {"ok": True, "transfer_id": transfer_id, "amount": amount}

    except Exception as e:
        logger.error("Collaborator payout FAILED: user=%s error=%s", user_id, e)

        sb.table("payout_batches").insert({
            "agent_id": user_id,
            "stripe_transfer_id": f"FAILED_{uuid.uuid4().hex[:8]}",
            "source_stripe_account": source_account,
            "amount": amount,
            "currency": currency,
            "commission_count": 0,
            "commission_ids": [],
            "status": "failed",
            "error_message": str(e)[:500],
            "notes": "Fixed collaborator payout - FAILED",
        }).execute()

        return {"ok": False, "error": str(e)}


def execute_all_payouts(*, force: bool = False) -> dict[str, Any]:
    """Calculate and execute all pending payouts. Called by daily cron.

    Handles both:
    1. Commission-based payouts (agents) — based on pending commissions
    2. Fixed-amount payouts (collaborators) — based on fixed_amount in profile
    """
    batches = calculate_pending_payouts(force=force)

    results = []
    total_paid = 0.0
    total_failed = 0

    # 1. Commission-based agent payouts
    for batch in batches:
        result = execute_payout(batch)
        results.append({
            "agent_id": batch["agent_id"],
            "type": "commission",
            "amount": batch["total_amount"],
            **result,
        })
        if result["ok"]:
            total_paid += batch["total_amount"]
        else:
            total_failed += 1

    # 2. Fixed-amount collaborator payouts
    collab_payouts = _calculate_collaborator_payouts(force=force)
    for collab in collab_payouts:
        result = _execute_collaborator_payout(collab)
        results.append({
            "agent_id": collab["user_id"],
            "type": "collaborator_fixed",
            "amount": collab["fixed_amount"],
            **result,
        })
        if result["ok"]:
            total_paid += collab["fixed_amount"]
        else:
            total_failed += 1

    total_count = len(batches) + len(collab_payouts)

    if total_count == 0:
        return {"ok": True, "message": "No payouts due", "count": 0}

    return {
        "ok": True,
        "message": f"Processed {total_count} payouts ({len(batches)} commissions, {len(collab_payouts)} collaborators)",
        "count": total_count,
        "total_paid": round(total_paid, 2),
        "failed": total_failed,
        "results": results,
    }


def get_agent_payout_history(user_id: str, limit: int = 20) -> list[dict]:
    """Return payout history for an agent."""
    batches = (
        sb.table("payout_batches")
        .select("*")
        .eq("agent_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return batches.data or []
