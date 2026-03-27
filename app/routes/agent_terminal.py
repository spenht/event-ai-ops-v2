"""Agent Payment Terminal — lets agents create charges from their dashboard."""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..deps import sb

logger = logging.getLogger("agent_terminal")

router = APIRouter(prefix="/v1/agent-terminal", tags=["agent-terminal"])


# ── Stripe key lookup ────────────────────────────────────────────
_STRIPE_KEY_MAP = {
    "stripe_lba": "STRIPE_KEY_LBA",
    "stripe_uvul": "STRIPE_KEY_UVUL",
    "stripe_oll": "STRIPE_KEY_OLL",
    "stripe_2clicks": "STRIPE_KEY_2CLICKS",
}


def _stripe_key_for(gateway_key: str) -> str:
    env_var = _STRIPE_KEY_MAP.get(gateway_key, "")
    return os.getenv(env_var, "") if env_var else ""


# ── 1. Terminal config ───────────────────────────────────────────
@router.get("/config")
async def get_terminal_config(request: Request):
    """Return projects, gateways, and commission rates for the logged-in agent."""
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user ID")

    # Projects this agent is assigned to
    agent_projects = (
        sb.table("project_agents")
        .select("*, projects(*)")
        .eq("user_id", user_id)
        .execute()
    )

    configs = []
    for pa in agent_projects.data or []:
        project = pa.get("projects") or {}
        project_id = pa["project_id"]

        # Payment gateways enabled for this project
        gateways = (
            sb.table("project_payment_gateways")
            .select("*")
            .eq("project_id", project_id)
            .execute()
        )

        # Campaigns linked to this project
        campaigns = (
            sb.table("campaign_projects")
            .select("campaign_id")
            .eq("project_id", project_id)
            .execute()
        )

        configs.append({
            "project_id": project_id,
            "project_name": project.get("name", ""),
            "commission_rate": pa.get("commission_rate", 0),
            "role": pa.get("role", "agent"),
            "gateways": gateways.data or [],
            "campaigns": [c["campaign_id"] for c in (campaigns.data or [])],
        })

    return {"ok": True, "data": configs}


# ── 2. Create charge ────────────────────────────────────────────
@router.post("/charge")
async def create_terminal_charge(request: Request):
    """Create a Stripe PaymentIntent, record the sale, and calculate commission."""
    body = await request.json()
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user ID")

    project_id = body.get("project_id")
    amount = body.get("amount")  # dollars, e.g. 79.00
    currency = body.get("currency", "USD")
    gateway_id = body.get("gateway_id")
    customer_email = body.get("customer_email", "")
    customer_name = body.get("customer_name", "")
    description = body.get("description", "")

    if not project_id or not amount or not gateway_id:
        raise HTTPException(status_code=400, detail="project_id, amount, and gateway_id are required")

    # 1. Verify agent is assigned to this project
    pa = (
        sb.table("project_agents")
        .select("*")
        .eq("project_id", project_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not pa.data:
        raise HTTPException(status_code=403, detail="Agent not assigned to this project")

    # 2. Get the payment gateway config
    gw = (
        sb.table("project_payment_gateways")
        .select("*")
        .eq("id", gateway_id)
        .eq("project_id", project_id)
        .execute()
    )
    if not gw.data:
        raise HTTPException(status_code=404, detail="Payment gateway not found")

    gateway = gw.data[0]
    stripe_key = _stripe_key_for(gateway.get("gateway_key", ""))
    if not stripe_key:
        raise HTTPException(
            status_code=400,
            detail=f"No Stripe key configured for gateway: {gateway.get('gateway_key')}",
        )

    # 3. Create Stripe PaymentIntent
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.stripe.com/v1/payment_intents",
            auth=(stripe_key, ""),
            data={
                "amount": int(float(amount) * 100),
                "currency": currency.lower(),
                "description": description or f"Sale by agent {user_id} - {customer_name}",
                "receipt_email": customer_email or None,
                "metadata[agent_id]": user_id,
                "metadata[project_id]": project_id,
                "metadata[gateway_id]": gateway_id,
                "metadata[source]": "agent_terminal",
                "automatic_payment_methods[enabled]": "true",
            },
        )
        if r.status_code != 200:
            logger.error("Stripe error: %s", r.text[:300])
            raise HTTPException(status_code=400, detail=f"Stripe error: {r.text[:200]}")
        pi = r.json()

    # 4. Record in financial_transactions
    now_iso = datetime.now(timezone.utc).isoformat()
    txn = {
        "external_id": pi["id"],
        "source": gateway.get("gateway_key", "agent_terminal"),
        "type": "sale",
        "amount": float(amount),
        "currency": currency.upper(),
        "txn_date": now_iso,
        "description": description or f"Agent terminal sale - {customer_name}",
        "counterparty": customer_name or customer_email,
        "project_id": project_id,
        "auto_assigned": True,
        "metadata": {
            "agent_id": user_id,
            "gateway_id": gateway_id,
            "customer_email": customer_email,
            "source": "agent_terminal",
            "payment_intent_id": pi["id"],
        },
    }
    sb.table("financial_transactions").upsert(txn, on_conflict="external_id,source").execute()

    # 5. Calculate and record commission
    commission_rate = pa.data[0].get("commission_rate", 0) or 0
    commission_amount = 0.0
    if commission_rate > 0:
        commission_amount = round(float(amount) * (commission_rate / 100), 2)
        sb.table("commissions").insert({
            "campaign_id": None,
            "agent_id": user_id,
            "lead_id": None,
            "call_record_id": None,
            "tier": "DIRECT_SALE",
            "sale_amount": float(amount),
            "commission_pct": commission_rate,
            "commission_amount": commission_amount,
            "status": "pending",
            "notes": f"Agent terminal sale - {description}",
            "created_at": now_iso,
        }).execute()

    return {
        "ok": True,
        "data": {
            "payment_intent_id": pi["id"],
            "client_secret": pi["client_secret"],
            "amount": float(amount),
            "currency": currency,
            "commission": {
                "rate": commission_rate,
                "amount": commission_amount,
            },
        },
    }


# ── 3. Sales history ────────────────────────────────────────────
@router.get("/sales")
async def get_agent_sales(request: Request):
    """Return the agent's sales and commission totals for a given period."""
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user ID")

    period = request.query_params.get("period", "30d")
    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": 3650}
    days = days_map.get(period, 30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        # Sales from financial_transactions where metadata contains agent_id
        sales_q = (
            sb.table("financial_transactions")
            .select("*")
            .filter("metadata->>agent_id", "eq", user_id)
            .gte("txn_date", cutoff)
            .order("txn_date", desc=True)
        )
        sales = (sales_q.execute()).data or []
    except Exception as e:
        logger.warning("sales query failed: %s", e)
        sales = []

    total_usd = sum(float(s["amount"]) for s in sales if s.get("currency") == "USD")
    total_mxn = sum(float(s["amount"]) for s in sales if s.get("currency") == "MXN")

    # Commissions
    try:
        comms = (
            sb.table("commissions")
            .select("*")
            .eq("agent_id", user_id)
            .gte("created_at", cutoff)
            .execute()
        )
        comms_data = comms.data or []
    except Exception as e:
        logger.warning("commissions query failed: %s", e)
        comms_data = []

    total_commission = sum(float(c.get("commission_amount") or 0) for c in comms_data)
    pending_commission = sum(float(c.get("commission_amount") or 0) for c in comms_data if c.get("status") == "pending")
    paid_commission = sum(float(c.get("commission_amount") or 0) for c in comms_data if c.get("status") == "paid")

    return {
        "ok": True,
        "data": {
            "sales": sales,
            "totals": {
                "count": len(sales),
                "usd": round(total_usd, 2),
                "mxn": round(total_mxn, 2),
            },
            "commissions": {
                "total": round(total_commission, 2),
                "pending": round(pending_commission, 2),
                "paid": round(paid_commission, 2),
            },
        },
    }


# ── 4. Leaderboard ──────────────────────────────────────────────
@router.get("/leaderboard")
async def get_sales_leaderboard(request: Request):
    """Sales leaderboard across all agents, optionally filtered by project."""
    project_id = request.query_params.get("project_id")
    period = request.query_params.get("period", "30d")

    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    days = days_map.get(period, 30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    q = (
        sb.table("financial_transactions")
        .select("*")
        .eq("type", "sale")
        .gte("txn_date", cutoff)
        .not_.is_("metadata->>agent_id", "null")
    )
    if project_id:
        q = q.eq("project_id", project_id)
    txns = (q.execute()).data or []

    # Group by agent
    agents: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd": 0.0, "total_mxn": 0.0})
    for s in txns:
        agent_id = (s.get("metadata") or {}).get("agent_id", "unknown")
        agents[agent_id]["count"] += 1
        if s.get("currency") == "USD":
            agents[agent_id]["total_usd"] += float(s["amount"])
        elif s.get("currency") == "MXN":
            agents[agent_id]["total_mxn"] += float(s["amount"])

    ranked = sorted(agents.items(), key=lambda x: x[1]["total_usd"], reverse=True)

    leaderboard = []
    for rank, (agent_id, stats) in enumerate(ranked, 1):
        leaderboard.append({
            "rank": rank,
            "agent_id": agent_id,
            "sales_count": stats["count"],
            "total_usd": round(stats["total_usd"], 2),
            "total_mxn": round(stats["total_mxn"], 2),
        })

    return {"ok": True, "data": leaderboard}
