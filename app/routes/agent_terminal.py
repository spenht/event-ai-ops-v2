"""Agent Payment Terminal — lets agents create charges from their dashboard."""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from ..deps import sb
from ..settings import settings

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
    is_admin = bool(request.headers.get("x-spartans-key"))
    if not user_id and not is_admin:
        raise HTTPException(status_code=401, detail="Missing user ID")

    # Check if admin/owner — they can access ALL projects
    if is_admin:
        all_projects = sb.table("projects").select("*").eq("status", "active").execute()
        project_list = all_projects.data or []
    else:
        # Regular agent — only assigned projects
        agent_projects = (
            sb.table("project_agents")
            .select("*, projects(*)")
            .eq("user_id", user_id)
            .execute()
        )
        project_list = []
        for pa in agent_projects.data or []:
            proj = pa.get("projects") or {}
            proj["_commission_rate"] = pa.get("commission_rate", 0)
            proj["_role"] = pa.get("role", "agent")
            project_list.append(proj)

    configs = []
    for project in project_list:
        project_id = project.get("id", "")
        if not project_id:
            continue

        # Payment gateways enabled for this project
        gateways = (
            sb.table("project_payment_gateways")
            .select("*")
            .eq("project_id", project_id)
            .execute()
        )

        # If no gateways configured, show default Stripe accounts based on project's stripe_account
        gw_data = gateways.data or []
        if not gw_data:
            stripe_acct = project.get("stripe_account", "")
            if stripe_acct:
                gw_data = [{
                    "id": f"default_{stripe_acct}",
                    "project_id": project_id,
                    "gateway_type": "stripe",
                    "gateway_key": stripe_acct,
                    "label": f"Stripe {stripe_acct.upper()}",
                    "is_primary": True,
                }]

        # Campaigns linked to this project (table may not exist yet)
        campaign_ids = []
        try:
            campaigns = (
                sb.table("campaign_projects")
                .select("campaign_id")
                .eq("project_id", project_id)
                .execute()
            )
            campaign_ids = [c["campaign_id"] for c in (campaigns.data or [])]
        except Exception:
            pass

        configs.append({
            "project_id": project_id,
            "project_name": project.get("name", ""),
            "commission_rate": project.get("_commission_rate", 0) if not is_admin else 0,
            "role": project.get("_role", "admin") if not is_admin else "admin",
            "gateways": gw_data,
            "campaigns": campaign_ids,
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


# ── 5. Payment Links ──────────────────────────────────────────
@router.post("/payment-link")
async def create_payment_link(request: Request):
    """Create a Stripe Payment Link for an agent to send to a lead."""
    user_id = request.headers.get("x-user-id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "Missing x-user-id"}, 401)

    body = await request.json()
    project_id = body.get("project_id")
    gateway_id = body.get("gateway_id")
    amount = body.get("amount")  # in dollars
    currency = body.get("currency", "USD").lower()
    description = body.get("description", "Payment")
    customer_name = body.get("customer_name", "")
    customer_email = body.get("customer_email", "")

    if not project_id or not gateway_id or not amount:
        return JSONResponse({"ok": False, "error": "project_id, gateway_id, and amount are required"}, 400)

    # Verify agent is assigned to this project
    pa = (
        sb.table("project_agents")
        .select("*")
        .eq("project_id", project_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not pa.data:
        return JSONResponse({"ok": False, "error": "Agent not assigned to this project"}, 403)

    # Get the gateway's Stripe key
    gw = (
        sb.table("project_payment_gateways")
        .select("*")
        .eq("id", gateway_id)
        .eq("project_id", project_id)
        .execute()
    )
    if not gw.data:
        return JSONResponse({"ok": False, "error": "Gateway not found"}, 404)

    gateway = gw.data[0]
    stripe_key = _stripe_key_for(gateway.get("gateway_key", ""))
    if not stripe_key:
        return JSONResponse({"ok": False, "error": f"Stripe key not configured for gateway: {gateway.get('gateway_key')}"}, 400)

    async with httpx.AsyncClient() as client:
        # Create a Stripe Product
        prod_r = await client.post(
            "https://api.stripe.com/v1/products",
            data={"name": description},
            auth=(stripe_key, ""),
        )
        if prod_r.status_code != 200:
            logger.error("Stripe product creation error: %s", prod_r.text[:300])
            return JSONResponse({"ok": False, "error": "Failed to create Stripe product"}, 400)
        product_id = prod_r.json()["id"]

        # Create a Price
        price_r = await client.post(
            "https://api.stripe.com/v1/prices",
            data={
                "product": product_id,
                "unit_amount": int(float(amount) * 100),
                "currency": currency,
            },
            auth=(stripe_key, ""),
        )
        if price_r.status_code != 200:
            logger.error("Stripe price creation error: %s", price_r.text[:300])
            return JSONResponse({"ok": False, "error": "Failed to create Stripe price"}, 400)
        price_id = price_r.json()["id"]

        # Create Payment Link
        link_data = {
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": 1,
            "metadata[agent_id]": user_id,
            "metadata[project_id]": project_id,
            "after_completion[type]": "redirect",
            "after_completion[redirect][url]": "https://legacybusinessacademy.com/gracias",
        }
        if customer_email:
            link_data["custom_fields[0][key]"] = "email"
            link_data["custom_fields[0][label][type]"] = "custom"
            link_data["custom_fields[0][label][custom]"] = "Email"
            link_data["custom_fields[0][type]"] = "text"

        link_r = await client.post(
            "https://api.stripe.com/v1/payment_links",
            data=link_data,
            auth=(stripe_key, ""),
        )

        if link_r.status_code != 200:
            err_msg = link_r.json().get("error", {}).get("message", "Unknown error")
            logger.error("Stripe payment link error: %s", err_msg)
            return JSONResponse({"ok": False, "error": err_msg}, 400)

        link = link_r.json()

        # Save the payment link record
        now_iso = datetime.now(timezone.utc).isoformat()
        sb.table("financial_transactions").insert({
            "external_id": f"pl_{link['id']}",
            "source": gateway.get("gateway_key", "agent_terminal"),
            "type": "sale",
            "amount": float(amount),
            "currency": currency.upper(),
            "txn_date": now_iso,
            "description": f"Payment Link: {description}",
            "counterparty": customer_name or customer_email,
            "project_id": project_id,
            "metadata": {
                "agent_id": user_id,
                "payment_link_id": link["id"],
                "payment_link_url": link["url"],
                "status": "pending",
                "customer_email": customer_email,
                "customer_name": customer_name,
                "source": "agent_terminal_link",
            },
        }).execute()

        return {
            "ok": True,
            "data": {
                "payment_link_url": link["url"],
                "payment_link_id": link["id"],
                "amount": float(amount),
                "currency": currency.upper(),
            },
        }


@router.get("/payment-links")
async def list_payment_links(request: Request):
    """List agent's created payment links with status."""
    user_id = request.headers.get("x-user-id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "Missing x-user-id"}, 401)

    project_id = request.query_params.get("project_id")

    try:
        q = (
            sb.table("financial_transactions")
            .select("*")
            .filter("metadata->>agent_id", "eq", user_id)
            .like("external_id", "pl_%")
            .order("txn_date", desc=True)
            .limit(100)
        )
        if project_id:
            q = q.eq("project_id", project_id)

        links = (q.execute()).data or []
    except Exception as e:
        logger.warning("payment links query failed: %s", e)
        links = []

    return {
        "ok": True,
        "data": {
            "links": [
                {
                    "id": lnk.get("id"),
                    "external_id": lnk.get("external_id"),
                    "amount": float(lnk.get("amount", 0)),
                    "currency": lnk.get("currency", "USD"),
                    "description": lnk.get("description", ""),
                    "counterparty": lnk.get("counterparty", ""),
                    "created_at": lnk.get("txn_date"),
                    "payment_link_url": (lnk.get("metadata") or {}).get("payment_link_url", ""),
                    "status": (lnk.get("metadata") or {}).get("status", "unknown"),
                    "customer_email": (lnk.get("metadata") or {}).get("customer_email", ""),
                }
                for lnk in links
            ],
            "count": len(links),
        },
    }


# ── 6. Admin Terminal Settings ─────────────────────────────────
def _check_admin(request: Request) -> bool:
    """Check admin access via x-spartans-key header."""
    key = (request.headers.get("x-spartans-key") or "").strip()
    return bool(key and settings.spartans_key and key == settings.spartans_key)


@router.get("/admin/settings")
async def admin_terminal_settings(request: Request):
    """Return all terminal configuration for admin view."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    # All projects with gateways
    projects = sb.table("projects").select("id, name, slug").execute()
    project_list = []
    for proj in projects.data or []:
        gateways = (
            sb.table("project_payment_gateways")
            .select("*")
            .eq("project_id", proj["id"])
            .execute()
        )
        project_list.append({
            **proj,
            "gateways": gateways.data or [],
        })

    # All agents with their assignments and commission rates
    agents_raw = (
        sb.table("project_agents")
        .select("*, projects(name)")
        .execute()
    )
    agents_by_user: dict[str, dict] = {}
    for pa in agents_raw.data or []:
        uid = pa["user_id"]
        if uid not in agents_by_user:
            agents_by_user[uid] = {
                "user_id": uid,
                "assignments": [],
            }
        agents_by_user[uid]["assignments"].append({
            "project_id": pa["project_id"],
            "project_name": (pa.get("projects") or {}).get("name", ""),
            "commission_rate": pa.get("commission_rate", 0),
            "role": pa.get("role", "agent"),
            "enabled": pa.get("enabled", True),
            "gateway_ids": pa.get("gateway_ids", []),
        })

    # Commission tiers
    tiers = sb.table("commission_tiers").select("*").execute()

    return {
        "ok": True,
        "data": {
            "projects": project_list,
            "agents": list(agents_by_user.values()),
            "commission_tiers": tiers.data or [],
        },
    }


@router.put("/admin/agent-access")
async def admin_update_agent_access(request: Request):
    """Admin enables/disables specific gateways for an agent."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    agent_id = body.get("agent_id")
    project_id = body.get("project_id")
    gateway_ids = body.get("gateway_ids", [])
    commission_rate = body.get("commission_rate")
    enabled = body.get("enabled", True)

    if not agent_id or not project_id:
        return JSONResponse({"ok": False, "error": "agent_id and project_id are required"}, 400)

    # Check if assignment exists
    existing = (
        sb.table("project_agents")
        .select("*")
        .eq("user_id", agent_id)
        .eq("project_id", project_id)
        .execute()
    )

    update_data: dict = {
        "gateway_ids": gateway_ids,
        "enabled": enabled,
    }
    if commission_rate is not None:
        update_data["commission_rate"] = commission_rate

    if existing.data:
        # Update existing assignment
        sb.table("project_agents").update(update_data).eq(
            "user_id", agent_id
        ).eq("project_id", project_id).execute()
    else:
        # Create new assignment
        sb.table("project_agents").insert({
            "user_id": agent_id,
            "project_id": project_id,
            "role": "agent",
            **update_data,
        }).execute()

    return {"ok": True, "message": "Agent access updated"}


@router.get("/admin/all-sales")
async def admin_all_sales(request: Request):
    """Admin view of ALL sales across all agents with filters."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    project_id = request.query_params.get("project_id")
    agent_id = request.query_params.get("agent_id")
    days = int(request.query_params.get("days", "30"))
    min_amount = request.query_params.get("min_amount")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    q = (
        sb.table("financial_transactions")
        .select("*")
        .eq("type", "sale")
        .gte("txn_date", cutoff)
        .not_.is_("metadata->>agent_id", "null")
        .order("txn_date", desc=True)
    )

    if project_id:
        q = q.eq("project_id", project_id)
    if agent_id:
        q = q.filter("metadata->>agent_id", "eq", agent_id)
    if min_amount:
        q = q.gte("amount", float(min_amount))

    txns = (q.execute()).data or []

    total_usd = sum(float(t["amount"]) for t in txns if t.get("currency") == "USD")
    total_mxn = sum(float(t["amount"]) for t in txns if t.get("currency") == "MXN")

    # Group by agent for summary
    agent_totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "usd": 0.0, "mxn": 0.0})
    for t in txns:
        aid = (t.get("metadata") or {}).get("agent_id", "unknown")
        agent_totals[aid]["count"] += 1
        if t.get("currency") == "USD":
            agent_totals[aid]["usd"] += float(t["amount"])
        elif t.get("currency") == "MXN":
            agent_totals[aid]["mxn"] += float(t["amount"])

    return {
        "ok": True,
        "data": {
            "transactions": txns,
            "totals": {
                "count": len(txns),
                "usd": round(total_usd, 2),
                "mxn": round(total_mxn, 2),
            },
            "by_agent": {
                aid: {"count": s["count"], "usd": round(s["usd"], 2), "mxn": round(s["mxn"], 2)}
                for aid, s in agent_totals.items()
            },
        },
    }


@router.post("/admin/commission-tiers")
async def admin_set_commission_tiers(request: Request):
    """Create/update commission tiers for a project."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    project_id = body.get("project_id")
    tiers = body.get("tiers", [])

    if not project_id:
        return JSONResponse({"ok": False, "error": "project_id is required"}, 400)
    if not tiers:
        return JSONResponse({"ok": False, "error": "tiers array is required"}, 400)

    # Delete existing tiers for this project
    sb.table("commission_tiers").delete().eq("project_id", project_id).execute()

    # Insert new tiers
    rows = []
    for tier in tiers:
        rows.append({
            "project_id": project_id,
            "min_sales": tier.get("min_sales", 0),
            "max_sales": tier.get("max_sales"),
            "rate": tier["rate"],
        })
    sb.table("commission_tiers").insert(rows).execute()

    return {
        "ok": True,
        "message": f"Set {len(rows)} commission tiers for project {project_id}",
        "data": rows,
    }
