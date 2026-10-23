"""Agent Payment Terminal — lets agents create charges + commission payouts."""
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


# ── Gateway key lookup ────────────────────────────────────────────
_GATEWAY_KEY_MAP = {
    "stripe_lba": "STRIPE_KEY_LBA",
    "stripe_uvul": "STRIPE_KEY_UVUL",
    "stripe_oll": "STRIPE_KEY_OLL",
    "stripe_2clicks": "STRIPE_KEY_2CLICKS",
    # Short keys (gateway_key values from DB)
    "lba": "STRIPE_KEY_LBA",
    "uvul": "STRIPE_KEY_UVUL",
    "oll": "STRIPE_KEY_OLL",
    "2clicks": "STRIPE_KEY_2CLICKS",
    # Whop
    "whop": "WHOP_API_KEY",
}

WHOP_COMPANY_ID = "biz_y0O8hypYRi8ZVv"


def _gateway_key_for(gateway_key: str) -> str:
    env_var = _GATEWAY_KEY_MAP.get(gateway_key, "")
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

        # Payment gateways enabled for this project (only enabled ones for agents)
        gw_query = (
            sb.table("project_payment_gateways")
            .select("*")
            .eq("project_id", project_id)
        )
        if not is_admin:
            gw_query = gw_query.eq("enabled", True)
        gateways = gw_query.execute()

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

        # Campaigns linked to this project (via campaigns.project_id column)
        campaign_ids = []
        try:
            campaigns = (
                sb.table("campaigns")
                .select("id, name, event_name")
                .eq("project_id", project_id)
                .eq("status", "active")
                .execute()
            )
            campaign_ids = [c["id"] for c in (campaigns.data or [])]
        except Exception:
            pass

        # Products for this project
        products_list = []
        try:
            products_q = (
                sb.table("project_products")
                .select("*")
                .eq("project_id", project_id)
                .eq("is_active", True)
                .order("sort_order")
            )
            products_result = products_q.execute()
            all_products = products_result.data or []

            if not is_admin and user_id:
                # Filter to products this agent has access to
                access = (
                    sb.table("agent_product_access")
                    .select("product_id, custom_commission_pct")
                    .eq("agent_id", user_id)
                    .execute()
                )
                access_map = {a["product_id"]: a.get("custom_commission_pct") for a in (access.data or [])}
                if access_map:
                    for p in all_products:
                        if p["id"] in access_map:
                            custom = access_map[p["id"]]
                            products_list.append({
                                **p,
                                "commission_pct": custom if custom is not None else p.get("commission_pct", 0),
                            })
                else:
                    # No specific access rules → agent sees all products
                    products_list = all_products
            else:
                products_list = all_products
        except Exception:
            pass

        configs.append({
            "id": project_id,
            "project_id": project_id,
            "name": project.get("name", ""),
            "project_name": project.get("name", ""),
            "commission_pct": project.get("_commission_rate", 0) if not is_admin else 0,
            "commission_rate": project.get("_commission_rate", 0) if not is_admin else 0,
            "role": project.get("_role", "admin") if not is_admin else "admin",
            "gateways": gw_data,
            "products": products_list,
            "campaigns": campaign_ids,
        })

    return {"ok": True, "data": {"projects": configs}}


# ── 2. Create charge ────────────────────────────────────────────
@router.post("/charge")
async def create_terminal_charge(request: Request):
    """Create a Stripe PaymentIntent, record the sale, and calculate commission."""
    body = await request.json()
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user ID")

    project_id = body.get("project_id")
    product_id = body.get("product_id")  # optional — if set, use product commission
    amount = body.get("amount")  # dollars, e.g. 79.00
    currency = body.get("currency", "USD")
    gateway_id = body.get("gateway_id")
    customer_email = body.get("customer_email", "")
    customer_name = body.get("customer_name", "")
    description = body.get("description", "")

    if not project_id or not amount or not gateway_id:
        raise HTTPException(status_code=400, detail="project_id, amount, and gateway_id are required")

    # 1. Verify agent is assigned to this project (admins bypass)
    is_admin = bool(request.headers.get("x-spartans-key"))
    pa_data: list[dict] = []
    if not is_admin:
        pa = (
            sb.table("project_agents")
            .select("*")
            .eq("project_id", project_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not pa.data:
            raise HTTPException(status_code=403, detail="Agent not assigned to this project")
        pa_data = pa.data
    else:
        pa_data = [{"commission_rate": 0}]

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
    gateway_type = gateway.get("gateway_type", "stripe")
    api_key = _gateway_key_for(gateway.get("gateway_key", ""))
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"No API key configured for gateway: {gateway.get('gateway_key')}",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    commission_rate = pa_data[0].get("commission_rate", 0) or 0

    # Debounce: reject if there's already a pending charge for same agent+project+amount in last 2 min
    if gateway_type != "whop":
        two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        try:
            recent = (
                sb.table("financial_transactions")
                .select("id, external_id")
                .eq("source", f"stripe_{gateway.get('gateway_key', '')}")
                .filter("metadata->>agent_id", "eq", user_id)
                .filter("metadata->>status", "eq", "pending")
                .eq("project_id", project_id)
                .eq("amount", float(amount))
                .gte("created_at", two_min_ago)
                .limit(1)
                .execute()
            )
            if recent.data:
                raise HTTPException(
                    status_code=429,
                    detail="Charge already in progress for this amount. Wait 2 minutes or use a different amount.",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("debounce_check_failed: %s", str(e)[:100])

    # Override commission from product if specified
    product_name = ""
    product_stripe_price_id = ""
    product_whop_plan_id = ""
    if product_id:
        try:
            prod = sb.table("project_products").select("*").eq("id", product_id).execute()
            if prod.data:
                prow = prod.data[0]
                product_name = prow.get("name", "")
                product_commission = prow.get("commission_pct")
                if product_commission is not None and product_commission > 0:
                    commission_rate = float(product_commission)
                # Check for agent-specific override
                if user_id and not is_admin:
                    access = (
                        sb.table("agent_product_access")
                        .select("custom_commission_pct")
                        .eq("agent_id", user_id)
                        .eq("product_id", product_id)
                        .execute()
                    )
                    if access.data and access.data[0].get("custom_commission_pct") is not None:
                        commission_rate = float(access.data[0]["custom_commission_pct"])
                if not description:
                    description = product_name
                if not amount or float(amount) == 0:
                    default_price = prow.get("default_price", 0)
                    if default_price:
                        amount = default_price
                        currency = prow.get("currency", currency)
                # Get Stripe/Whop IDs for this gateway
                gw_key = gateway.get("gateway_key", "")
                stripe_price_ids = prow.get("stripe_price_ids") or {}
                if isinstance(stripe_price_ids, dict):
                    product_stripe_price_id = stripe_price_ids.get(gw_key, "")
                product_whop_plan_id = prow.get("whop_plan_id", "") or ""
        except Exception as e:
            logger.warning("product lookup failed: %s", e)

    if gateway_type == "whop":
        # ── Whop checkout flow ──────────────────────────────────
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.whop.com/api/v1/checkout_configurations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "plan": {
                        "company_id": WHOP_COMPANY_ID,
                        "currency": currency.lower(),
                        "initial_price": float(amount),
                        "plan_type": "one_time",
                    },
                    "metadata": {
                        "agent_id": user_id,
                        "project_id": project_id,
                        "customer_email": customer_email,
                        "customer_name": customer_name,
                        "source": "agent_terminal",
                    },
                    "redirect_url": "https://legacybusinessacademy.com/gracias",
                },
            )
            if r.status_code >= 400:
                logger.error("Whop error: %s", r.text[:300])
                raise HTTPException(status_code=400, detail=f"Whop error: {r.text[:200]}")
            checkout = r.json().get("data", r.json())

        checkout_url = checkout.get("purchase_url", "")
        checkout_id = checkout.get("id", "")

        # Record in financial_transactions
        txn = {
            "external_id": f"whop_{checkout_id}",
            "source": "whop",
            "type": "sale",
            "amount": float(amount),
            "currency": currency.upper(),
            "txn_date": now_iso,
            "description": description or f"Whop checkout - {customer_name}",
            "counterparty": customer_name or customer_email,
            "project_id": project_id,
            "auto_assigned": True,
            "metadata": {
                "agent_id": user_id,
                "gateway_id": gateway_id,
                "customer_email": customer_email,
                "checkout_url": checkout_url,
                "checkout_id": checkout_id,
                "status": "pending",
                "source": "agent_terminal",
            },
        }
        sb.table("financial_transactions").upsert(txn, on_conflict="external_id,source").execute()

        # Commission
        commission_amount = 0.0
        if commission_rate > 0:
            commission_amount = round(float(amount) * (commission_rate / 100), 2)

        return {
            "ok": True,
            "data": {
                "gateway_type": "whop",
                "checkout_url": checkout_url,
                "checkout_id": checkout_id,
                "amount": float(amount),
                "currency": currency,
                "commission": {"rate": commission_rate, "amount": commission_amount},
            },
        }

    # ── Stripe checkout flow (default) ──────────────────────────
    # ── Stripe checkout flow (default) ──────────────────────────
    async with httpx.AsyncClient() as client:
        stripe_data: dict[str, str | None] = {
            "amount": str(int(float(amount) * 100)),
            "currency": currency.lower(),
            "description": description or f"Sale by agent {user_id} - {customer_name}",
            "receipt_email": customer_email or None,
            "metadata[agent_id]": user_id,
            "metadata[project_id]": project_id,
            "metadata[gateway_id]": gateway_id,
            "metadata[source]": "agent_terminal",
            "automatic_payment_methods[enabled]": "true",
        }
        if product_id:
            stripe_data["metadata[product_id]"] = product_id
        if product_name:
            stripe_data["metadata[product_name]"] = product_name

        r = await client.post(
            "https://api.stripe.com/v1/payment_intents",
            auth=(api_key, ""),
            data=stripe_data,
        )
        if r.status_code != 200:
            logger.error("Stripe error: %s", r.text[:300])
            raise HTTPException(status_code=400, detail=f"Stripe error: {r.text[:200]}")
        pi = r.json()

    # Record in financial_transactions (source aligned with webhook/sync: stripe_xxx)
    gw_key = gateway.get("gateway_key", "agent_terminal")
    txn = {
        "external_id": pi["id"],
        "source": f"stripe_{gw_key}",
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
            "gateway_key": gw_key,
            "customer_email": customer_email,
            "source": "agent_terminal",
            "status": "pending",
            "payment_intent_id": pi["id"],
            "product_id": product_id or "",
            "product_name": product_name or "",
        },
    }
    sb.table("financial_transactions").upsert(txn, on_conflict="external_id,source").execute()

    # Commission is calculated here for display but NOT inserted yet.
    # Commission row is created by the webhook when payment_intent.succeeded fires,
    # preventing phantom commissions from charges that never complete.
    commission_amount = 0.0
    if commission_rate > 0:
        commission_amount = round(float(amount) * (commission_rate / 100), 2)

    return {
        "ok": True,
        "data": {
            "gateway_type": "stripe",
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

    # Filter out stale pending charges (older than 30 min without payment confirmation)
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    sales = [
        s for s in sales
        if (s.get("metadata") or {}).get("status") != "pending"
        or s.get("created_at", "") >= stale_cutoff
    ]

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

    # Verify agent is assigned to this project (admins bypass)
    is_admin = bool(request.headers.get("x-spartans-key"))
    if not is_admin:
        pa = (
            sb.table("project_agents")
            .select("*")
            .eq("project_id", project_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not pa.data:
            return JSONResponse({"ok": False, "error": "Agent not assigned to this project"}, 403)

    # Get the gateway config
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
    gateway_type = gateway.get("gateway_type", "stripe")
    api_key = _gateway_key_for(gateway.get("gateway_key", ""))
    if not api_key:
        return JSONResponse({"ok": False, "error": f"API key not configured for gateway: {gateway.get('gateway_key')}"}, 400)

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Whop payment link ─────────────────────────────────────
    if gateway_type == "whop":
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.whop.com/api/v1/checkout_configurations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "plan": {
                        "company_id": WHOP_COMPANY_ID,
                        "currency": currency,
                        "initial_price": float(amount),
                        "plan_type": "one_time",
                    },
                    "metadata": {
                        "agent_id": user_id,
                        "project_id": project_id,
                        "customer_email": customer_email,
                        "customer_name": customer_name,
                        "description": description,
                        "source": "agent_terminal_link",
                    },
                    "redirect_url": "https://legacybusinessacademy.com/gracias",
                },
            )
            if r.status_code >= 400:
                logger.error("Whop payment link error: %s", r.text[:300])
                return JSONResponse({"ok": False, "error": f"Whop error: {r.text[:200]}"}, 400)

            checkout = r.json().get("data", r.json())
            checkout_url = checkout.get("purchase_url", "")
            checkout_id = checkout.get("id", "")

        sb.table("financial_transactions").insert({
            "external_id": f"pl_whop_{checkout_id}",
            "source": "whop",
            "type": "sale",
            "amount": float(amount),
            "currency": currency.upper(),
            "txn_date": now_iso,
            "description": f"Payment Link: {description}",
            "counterparty": customer_name or customer_email,
            "project_id": project_id,
            "metadata": {
                "agent_id": user_id,
                "payment_link_url": checkout_url,
                "checkout_id": checkout_id,
                "status": "pending",
                "customer_email": customer_email,
                "customer_name": customer_name,
                "source": "agent_terminal_link",
            },
        }).execute()

        return {
            "ok": True,
            "data": {
                "gateway_type": "whop",
                "payment_link_url": checkout_url,
                "payment_link_id": checkout_id,
                "amount": float(amount),
                "currency": currency.upper(),
            },
        }

    # ── Stripe payment link (default) ─────────────────────────
    async with httpx.AsyncClient() as client:
        prod_r = await client.post(
            "https://api.stripe.com/v1/products",
            data={"name": description},
            auth=(api_key, ""),
        )
        if prod_r.status_code != 200:
            logger.error("Stripe product creation error: %s", prod_r.text[:300])
            return JSONResponse({"ok": False, "error": "Failed to create Stripe product"}, 400)
        product_id = prod_r.json()["id"]

        price_r = await client.post(
            "https://api.stripe.com/v1/prices",
            data={
                "product": product_id,
                "unit_amount": int(float(amount) * 100),
                "currency": currency,
            },
            auth=(api_key, ""),
        )
        if price_r.status_code != 200:
            logger.error("Stripe price creation error: %s", price_r.text[:300])
            return JSONResponse({"ok": False, "error": "Failed to create Stripe price"}, 400)
        price_id = price_r.json()["id"]

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
            auth=(api_key, ""),
        )

        if link_r.status_code != 200:
            err_msg = link_r.json().get("error", {}).get("message", "Unknown error")
            logger.error("Stripe payment link error: %s", err_msg)
            return JSONResponse({"ok": False, "error": err_msg}, 400)

        link = link_r.json()

        sb.table("financial_transactions").insert({
            "external_id": f"pl_{link['id']}",
            "source": f"stripe_{gateway.get('gateway_key', 'agent_terminal')}",
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
                "gateway_type": "stripe",
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


def _check_owner(request: Request) -> bool:
    """Check if caller is the platform owner (spartans key)."""
    return _check_admin(request)


async def _check_payout_permission(request: Request) -> bool:
    """Check if the user has payout management permission.

    Owner (spartans key) always has access.
    Admins/members need the ``can_manage_payouts`` flag in org_members.permissions.
    """
    key = (request.headers.get("x-spartans-key") or "").strip()
    # Owner always has access
    if key and settings.spartans_key and key == settings.spartans_key:
        return True
    # Check user's permissions via JWT
    auth_header = request.headers.get("authorization", "")
    if auth_header:
        try:
            import jwt as pyjwt

            token = auth_header.replace("Bearer ", "")
            decoded = pyjwt.decode(token, options={"verify_signature": False})
            uid = decoded.get("sub", "")
            if uid:
                member = (
                    sb.table("org_members")
                    .select("role, permissions")
                    .eq("user_id", uid)
                    .execute()
                )
                if member.data:
                    role = member.data[0].get("role", "")
                    if role == "owner":
                        return True
                    perms = member.data[0].get("permissions") or {}
                    return perms.get("can_manage_payouts", False)
        except Exception:
            pass
    return False


@router.get("/admin/settings")
async def admin_terminal_settings(request: Request):
    """Return all terminal configuration for admin view."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    # All projects with gateways
    projects = sb.table("projects").select("id, name").execute()

    # Get all agents from org_members (role=agent) + campaign_members for assignments
    org_agents = sb.table("org_members").select("user_id, role").eq("role", "agent").execute()
    agent_campaigns = sb.table("campaign_members").select("user_id, campaign_id, campaigns(name)").execute()

    # Build agent-to-campaigns map
    agent_campaign_map: dict[str, list[str]] = defaultdict(list)
    for ac in agent_campaigns.data or []:
        cname = (ac.get("campaigns") or {}).get("name", "")
        if cname:
            agent_campaign_map[ac["user_id"]].append(cname)

    agent_count_by_project: dict[str, int] = defaultdict(int)

    # Commission tiers by project
    try:
        tiers = sb.table("project_commission_tiers").select("*").execute()
        tier_projects = {t["project_id"] for t in (tiers.data or [])}
    except Exception:
        tiers = type("X", (), {"data": []})()
        tier_projects = set()

    project_list = []
    for proj in projects.data or []:
        pid = proj["id"]
        gateways = (
            sb.table("project_payment_gateways")
            .select("*")
            .eq("project_id", pid)
            .execute()
        )
        gw_list = [
            {
                "id": gw["id"],
                "label": gw.get("label", gw.get("gateway_key", "")),
                "enabled": gw.get("enabled", True),
            }
            for gw in (gateways.data or [])
        ]
        project_list.append({
            "id": pid,
            "name": proj["name"],
            "gateways": gw_list,
            "commission_pct": 0,
            "tier_enabled": pid in tier_projects,
            "agent_count": agent_count_by_project.get(pid, 0),
        })

    # Agents list — pull from org_members (role=agent) + auth emails
    # Fetch auth user emails via Supabase Admin API
    email_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    try:
        import httpx as _httpx
        auth_url = f"{os.getenv('SUPABASE_URL', '')}/auth/v1/admin/users?per_page=500"
        auth_headers = {
            "apikey": os.getenv("SUPABASE_KEY", ""),
            "Authorization": f"Bearer {os.getenv('SUPABASE_KEY', '')}",
        }
        with _httpx.Client(timeout=10) as hc:
            auth_r = hc.get(auth_url, headers=auth_headers)
            if auth_r.status_code == 200:
                auth_data = auth_r.json()
                for u in auth_data.get("users", []):
                    email_map[u["id"]] = u.get("email", "")
                    meta = u.get("user_metadata") or {}
                    name_map[u["id"]] = meta.get("full_name", meta.get("name", ""))
    except Exception as e:
        logger.warning("Failed to fetch auth users: %s", e)

    # Also check agent_payout_profiles for custom names (override auth-derived names)
    try:
        payout_profiles = sb.table("agent_payout_profiles").select("user_id, name").execute()
        for pp in payout_profiles.data or []:
            if pp.get("name"):
                name_map[pp["user_id"]] = pp["name"]
    except Exception as e:
        logger.warning("Failed to fetch payout profiles for names: %s", e)

    agents_list = []
    for oa in org_agents.data or []:
        uid = oa["user_id"]
        campaigns = agent_campaign_map.get(uid, [])
        email = email_map.get(uid, "")
        name = name_map.get(uid, "") or email.split("@")[0] if email else uid[:12]
        agents_list.append({
            "id": uid,
            "name": name,
            "email": email,
            "project_id": "",
            "project_name": ", ".join(campaigns[:2]) if campaigns else "Unassigned",
            "commission_pct": 0,
            "sales_30d": 0,
            "status": "active",
        })

    return {
        "ok": True,
        "data": {
            "projects": project_list,
            "agents": agents_list,
            "commission_tiers": tiers.data or [],
        },
    }


@router.put("/admin/agent-name")
async def admin_update_agent_name(request: Request):
    """Update an agent's display name via agent_payout_profiles."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)
    body = await request.json()
    user_id = body.get("user_id")
    name = body.get("name", "")
    if not user_id or not name:
        return JSONResponse({"ok": False, "error": "user_id and name required"}, 400)

    # Update or create agent_payout_profiles with the name
    existing = sb.table("agent_payout_profiles").select("id").eq("user_id", user_id).execute()
    if existing.data:
        sb.table("agent_payout_profiles").update({"name": name}).eq("user_id", user_id).execute()
    else:
        sb.table("agent_payout_profiles").insert({"user_id": user_id, "name": name}).execute()

    return {"ok": True, "message": f"Name updated to {name}"}


@router.put("/admin/gateway-toggle")
async def admin_toggle_gateway(request: Request):
    """Enable or disable a gateway for a project."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    gateway_id = body.get("gateway_id")
    enabled = body.get("enabled")

    if not gateway_id or enabled is None:
        return JSONResponse({"ok": False, "error": "gateway_id and enabled are required"}, 400)

    sb.table("project_payment_gateways").update(
        {"enabled": enabled}
    ).eq("id", gateway_id).execute()

    return {"ok": True, "message": f"Gateway {'enabled' if enabled else 'disabled'}"}


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
    min_amount = request.query_params.get("min_amount")

    # Support both "days=30" and "period=30d" formats
    period = request.query_params.get("period", "")
    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    days = days_map.get(period, int(request.query_params.get("days", "30")))

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

    # Exclude stale pending charges (never completed)
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    txns = [
        t for t in txns
        if (t.get("metadata") or {}).get("status") != "pending"
        or t.get("created_at", "") >= stale_cutoff
    ]

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

    # Build top performers list sorted by USD amount
    ranked = sorted(agent_totals.items(), key=lambda x: x[1]["usd"], reverse=True)
    top_performers = [
        {
            "rank": i + 1,
            "name": aid,
            "amount": round(stats["usd"], 2),
            "project": "",
        }
        for i, (aid, stats) in enumerate(ranked[:10])
    ]

    total_sales = round(total_usd + total_mxn, 2)
    active_agents = len(agent_totals)

    return {
        "ok": True,
        "data": {
            "total_sales": total_sales,
            "total_commissions": 0,
            "active_agents": active_agents,
            "avg_per_agent": round(total_sales / active_agents, 2) if active_agents else 0,
            "top_performers": top_performers,
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


@router.get("/admin/commission-tiers")
async def admin_get_commission_tiers(request: Request):
    """Get commission tiers for a project."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    project_id = request.query_params.get("project_id")
    if not project_id:
        return JSONResponse({"ok": False, "error": "project_id required"}, 400)

    try:
        tiers = (
            sb.table("project_commission_tiers")
            .select("*")
            .eq("project_id", project_id)
            .order("sort_order")
            .execute()
        )
        return {"ok": True, "data": tiers.data or []}
    except Exception:
        return {"ok": True, "data": []}


@router.post("/admin/commission-tiers")
async def admin_set_commission_tiers(request: Request):
    """Create/update/delete commission tiers for a project."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    project_id = body.get("project_id")
    action = body.get("action", "bulk")  # create, update, delete, or bulk

    if not project_id:
        return JSONResponse({"ok": False, "error": "project_id is required"}, 400)

    if action == "create":
        row = {
            "project_id": project_id,
            "product_id": body.get("product_id"),  # NULL = project-wide
            "tier_metric": body.get("tier_metric", "amount"),  # "amount" or "units"
            "min_sales": body.get("min_sales", 0),
            "max_sales": body.get("max_sales"),
            "commission_pct": body.get("commission_pct", 0),
            "sort_order": body.get("sort_order", 0),
            "tier_mode": body.get("tier_mode", "flat"),
        }
        result = sb.table("project_commission_tiers").insert(row).execute()
        return {"ok": True, "data": result.data[0] if result.data else row}

    elif action == "update":
        tier_id = body.get("tier_id")
        if not tier_id:
            return JSONResponse({"ok": False, "error": "tier_id required for update"}, 400)
        update = {}
        for f in ["min_sales", "max_sales", "commission_pct", "sort_order", "tier_mode", "product_id", "tier_metric"]:
            if f in body:
                update[f] = body[f]
        sb.table("project_commission_tiers").update(update).eq("id", tier_id).execute()
        return {"ok": True, "message": "Tier updated"}

    elif action == "delete":
        tier_id = body.get("tier_id")
        if not tier_id:
            return JSONResponse({"ok": False, "error": "tier_id required for delete"}, 400)
        sb.table("project_commission_tiers").delete().eq("id", tier_id).execute()
        return {"ok": True, "message": "Tier deleted"}

    else:
        # Bulk replace
        tiers = body.get("tiers", [])
        if not tiers:
            return JSONResponse({"ok": False, "error": "tiers array required"}, 400)
        try:
            sb.table("project_commission_tiers").delete().eq("project_id", project_id).execute()
        except Exception:
            pass
        rows = []
        for i, tier in enumerate(tiers):
            rows.append({
                "project_id": project_id,
                "min_sales": tier.get("min_sales", 0),
                "max_sales": tier.get("max_sales"),
                "commission_pct": tier.get("rate", tier.get("commission_pct", 0)),
                "sort_order": i,
            })
        sb.table("project_commission_tiers").insert(rows).execute()
        return {"ok": True, "message": f"Set {len(rows)} tiers", "data": rows}


# ── 8. Agent Connect Onboarding ───────────────────────────────
@router.post("/connect-account")
async def agent_connect_account(request: Request):
    """Create/resume Stripe Express onboarding for an agent."""
    from ..services.agent_payouts import create_agent_connect_account

    user_id = request.headers.get("x-user-id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "Missing x-user-id"}, 401)

    body = await request.json()
    email = body.get("email", "")
    name = body.get("name", "")
    country = body.get("country", "US")

    if not email:
        return JSONResponse({"ok": False, "error": "email is required"}, 400)

    try:
        result = create_agent_connect_account(
            user_id=user_id, email=email, name=name, country=country,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        logger.error("Connect account creation failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@router.get("/connect-status")
async def agent_connect_status(request: Request):
    """Check agent's Stripe Connect onboarding status."""
    from ..services.agent_payouts import get_agent_connect_status

    user_id = request.headers.get("x-user-id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "Missing x-user-id"}, 401)

    result = get_agent_connect_status(user_id)
    return {"ok": True, "data": result}


@router.get("/payout-history")
async def agent_payout_history(request: Request):
    """Return agent's payout history."""
    from ..services.agent_payouts import get_agent_payout_history

    user_id = request.headers.get("x-user-id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "Missing x-user-id"}, 401)

    history = get_agent_payout_history(user_id)
    return {"ok": True, "data": history}


# ── 9. Admin Payout Management ───────────────────────────────
@router.get("/admin/pending-payouts")
async def admin_pending_payouts(request: Request):
    """Preview pending payouts without executing them."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    from ..services.agent_payouts import calculate_pending_payouts

    batches = calculate_pending_payouts(force=True)
    summary = [
        {
            "agent_id": b["agent_id"],
            "source": b["source_stripe_account"],
            "amount": round(b["total_amount"], 2),
            "commission_count": len(b["commissions"]),
            "currency": b.get("currency", "USD"),
        }
        for b in batches
    ]
    return {"ok": True, "data": summary, "total_batches": len(batches)}


@router.post("/admin/run-payouts")
async def admin_run_payouts(request: Request):
    """Execute all pending commission payouts."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    from ..services.agent_payouts import execute_all_payouts

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    force = body.get("force", False)

    result = execute_all_payouts(force=force)
    return {"ok": True, "data": result}


@router.put("/admin/payout-frequency")
async def admin_set_payout_frequency(request: Request):
    """Set payout frequency for an agent (daily/weekly/biweekly/monthly/manual)."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    user_id = body.get("user_id")
    frequency = body.get("frequency")

    valid = {"daily", "weekly", "biweekly", "monthly", "manual"}
    if not user_id or frequency not in valid:
        return JSONResponse({"ok": False, "error": f"user_id required, frequency must be one of {valid}"}, 400)

    sb.table("agent_payout_profiles").update(
        {"payout_frequency": frequency}
    ).eq("user_id", user_id).execute()

    return {"ok": True, "message": f"Payout frequency set to {frequency}"}


# ── 10. Product Management ──────────────────────────────────────

@router.get("/admin/products")
async def admin_list_products(request: Request):
    """List all products for a project."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    project_id = request.query_params.get("project_id")
    if not project_id:
        return JSONResponse({"ok": False, "error": "project_id required"}, 400)

    products = (
        sb.table("project_products")
        .select("*")
        .eq("project_id", project_id)
        .order("sort_order")
        .execute()
    )
    return {"ok": True, "data": products.data or []}


@router.post("/admin/products")
async def admin_create_product(request: Request):
    """Create a product and sync to Stripe/Whop."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    project_id = body.get("project_id")
    name = body.get("name", "")
    if not project_id or not name:
        return JSONResponse({"ok": False, "error": "project_id and name required"}, 400)

    default_price = body.get("default_price", 0)
    currency = body.get("currency", "USD").lower()
    description = body.get("description", "")

    # Get enabled gateways for this project
    gateways = (
        sb.table("project_payment_gateways")
        .select("*")
        .eq("project_id", project_id)
        .eq("enabled", True)
        .execute()
    )

    stripe_product_ids: dict[str, str] = {}
    stripe_price_ids: dict[str, str] = {}
    whop_plan_id = None
    sync_errors: list[str] = []

    async with httpx.AsyncClient() as client:
        for gw in gateways.data or []:
            gw_type = gw.get("gateway_type", "stripe")
            gw_key_name = gw.get("gateway_key", "")
            api_key = _gateway_key_for(gw_key_name)
            if not api_key:
                continue

            if gw_type == "stripe":
                try:
                    # Create Stripe Product
                    prod_r = await client.post(
                        "https://api.stripe.com/v1/products",
                        auth=(api_key, ""),
                        data={
                            "name": name,
                            "description": description or name,
                            "metadata[project_id]": project_id,
                            "metadata[source]": "agent_terminal",
                        },
                    )
                    if prod_r.status_code == 200:
                        sp_id = prod_r.json()["id"]
                        stripe_product_ids[gw_key_name] = sp_id

                        # Create Stripe Price (if default_price > 0)
                        if default_price and float(default_price) > 0:
                            price_r = await client.post(
                                "https://api.stripe.com/v1/prices",
                                auth=(api_key, ""),
                                data={
                                    "product": sp_id,
                                    "unit_amount": int(float(default_price) * 100),
                                    "currency": currency,
                                },
                            )
                            if price_r.status_code == 200:
                                stripe_price_ids[gw_key_name] = price_r.json()["id"]
                    else:
                        sync_errors.append(f"Stripe {gw_key_name}: {prod_r.text[:100]}")
                except Exception as e:
                    sync_errors.append(f"Stripe {gw_key_name}: {e}")

            elif gw_type == "whop" and not whop_plan_id:
                try:
                    # Create Whop plan via checkout configuration (one-time)
                    r = await client.post(
                        "https://api.whop.com/api/v1/checkout_configurations",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "plan": {
                                "company_id": WHOP_COMPANY_ID,
                                "currency": currency,
                                "initial_price": float(default_price) if default_price else 0,
                                "plan_type": "one_time",
                            },
                            "metadata": {
                                "product_name": name,
                                "project_id": project_id,
                                "source": "agent_terminal",
                            },
                        },
                    )
                    if r.status_code < 400:
                        checkout = r.json().get("data", r.json())
                        whop_plan_id = checkout.get("id", "")
                    else:
                        sync_errors.append(f"Whop: {r.text[:100]}")
                except Exception as e:
                    sync_errors.append(f"Whop: {e}")

    row = {
        "project_id": project_id,
        "name": name,
        "description": description,
        "default_price": default_price,
        "currency": currency.upper(),
        "commission_pct": body.get("commission_pct", 0),
        "is_active": body.get("is_active", True),
        "sort_order": body.get("sort_order", 0),
        "stripe_product_ids": stripe_product_ids,
        "stripe_price_ids": stripe_price_ids,
        "whop_plan_id": whop_plan_id,
    }
    result = sb.table("project_products").insert(row).execute()

    data = result.data[0] if result.data else row
    data["sync_errors"] = sync_errors
    return {"ok": True, "data": data}


@router.put("/admin/products/{product_id}")
async def admin_update_product(product_id: str, request: Request):
    """Update a product."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    update = {}
    for field in ["name", "description", "default_price", "currency", "commission_pct", "is_active", "sort_order"]:
        if field in body:
            update[field] = body[field]

    if not update:
        return JSONResponse({"ok": False, "error": "No fields to update"}, 400)

    sb.table("project_products").update(update).eq("id", product_id).execute()
    return {"ok": True, "message": "Product updated"}


@router.delete("/admin/products/{product_id}")
async def admin_delete_product(product_id: str, request: Request):
    """Delete a product."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    sb.table("project_products").delete().eq("id", product_id).execute()
    return {"ok": True, "message": "Product deleted"}


@router.get("/admin/product-access")
async def admin_get_product_access(request: Request):
    """Return agent assignments for a specific product."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)
    product_id = request.query_params.get("product_id")
    if not product_id:
        return JSONResponse({"ok": False, "error": "product_id required"}, 400)
    access = sb.table("agent_product_access").select("*").eq("product_id", product_id).execute()
    return {"ok": True, "data": access.data or []}


@router.post("/admin/product-access")
async def admin_set_product_access(request: Request):
    """Assign/remove agent access to a product."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    agent_id = body.get("agent_id")
    product_id = body.get("product_id")
    action = body.get("action", "add")  # add or remove

    if not agent_id or not product_id:
        return JSONResponse({"ok": False, "error": "agent_id and product_id required"}, 400)

    if action == "remove":
        sb.table("agent_product_access").delete().eq("agent_id", agent_id).eq("product_id", product_id).execute()
        return {"ok": True, "message": "Access removed"}

    custom_pct = body.get("custom_commission_pct")
    sb.table("agent_product_access").upsert({
        "agent_id": agent_id,
        "product_id": product_id,
        "custom_commission_pct": custom_pct,
    }, on_conflict="agent_id,product_id").execute()
    return {"ok": True, "message": "Access granted"}


# ── 10b. Admin Permission Management ─────────────────────────────


@router.get("/admin/user-permissions")
async def get_user_permissions(request: Request):
    """Get permissions for a specific user."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)
    user_id = request.query_params.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "user_id required"}, 400)
    member = sb.table("org_members").select("permissions").eq("user_id", user_id).execute()
    perms = (member.data[0].get("permissions") or {}) if member.data else {}
    return {"ok": True, "data": perms}


@router.put("/admin/user-permissions")
async def set_user_permissions(request: Request):
    """Set permissions for a user. OWNER ONLY."""
    if not _check_owner(request):
        return JSONResponse({"ok": False, "error": "Unauthorized — owner only"}, 401)

    body = await request.json()
    user_id = body.get("user_id")
    permissions = body.get("permissions", {})

    if not user_id:
        return JSONResponse({"ok": False, "error": "user_id required"}, 400)

    # Merge with existing permissions
    existing = sb.table("org_members").select("permissions").eq("user_id", user_id).execute()
    current_perms = (existing.data[0].get("permissions") or {}) if existing.data else {}
    current_perms.update(permissions)

    sb.table("org_members").update({"permissions": current_perms}).eq("user_id", user_id).execute()
    return {"ok": True, "data": current_perms}


@router.get("/admin/admin-members")
async def get_admin_members(request: Request):
    """List non-agent org members (admins/owners) with their permissions."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    members = (
        sb.table("org_members")
        .select("user_id, role, permissions")
        .neq("role", "agent")
        .execute()
    )

    # Fetch emails/names from auth
    email_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    try:
        import httpx as _httpx

        auth_url = f"{os.getenv('SUPABASE_URL', '')}/auth/v1/admin/users?per_page=500"
        auth_headers = {
            "apikey": os.getenv("SUPABASE_KEY", ""),
            "Authorization": f"Bearer {os.getenv('SUPABASE_KEY', '')}",
        }
        with _httpx.Client(timeout=10) as hc:
            auth_r = hc.get(auth_url, headers=auth_headers)
            if auth_r.status_code == 200:
                auth_data = auth_r.json()
                for u in auth_data.get("users", []):
                    email_map[u["id"]] = u.get("email", "")
                    meta = u.get("user_metadata") or {}
                    name_map[u["id"]] = meta.get("full_name", meta.get("name", ""))
    except Exception as e:
        logger.warning("Failed to fetch auth users for admin-members: %s", e)

    result = []
    for m in members.data or []:
        uid = m["user_id"]
        email = email_map.get(uid, "")
        name = name_map.get(uid, "") or (email.split("@")[0] if email else uid[:12])
        result.append({
            "user_id": uid,
            "role": m.get("role", ""),
            "name": name,
            "email": email,
            "permissions": m.get("permissions") or {},
        })

    return {"ok": True, "data": result}


# ── 11. Collaborator (Fixed Payout) Management ──────────────────


@router.get("/admin/collaborators")
async def admin_list_collaborators(request: Request):
    """List all collaborators with their fixed payout config."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    # Get all collaborator payout profiles
    profiles = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("profile_type", "collaborator")
        .execute()
    )

    collaborators = []
    for p in profiles.data or []:
        collaborators.append({
            "user_id": p.get("user_id", ""),
            "name": p.get("name", ""),
            "email": p.get("email", ""),
            "fixed_amount": float(p.get("fixed_amount", 0)),
            "currency": p.get("currency", "USD"),
            "payout_frequency": p.get("payout_frequency", "biweekly"),
            "source_stripe_account": p.get("source_stripe_account", "lba"),
            "stripe_connect_status": p.get("stripe_connect_status", "not_connected"),
            "stripe_connect_account_id": p.get("stripe_connect_account_id", ""),
            "created_at": p.get("created_at", ""),
        })

    return {"ok": True, "data": collaborators}


@router.post("/admin/collaborators")
async def admin_add_collaborator(request: Request):
    """Add a collaborator with fixed payout amount."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    body = await request.json()
    user_id = body.get("user_id", "")
    email = body.get("email", "")
    name = body.get("name", "")
    fixed_amount = body.get("fixed_amount", 0)
    currency = body.get("currency", "USD")
    payout_frequency = body.get("payout_frequency", "biweekly")
    source_stripe_account = body.get("source_stripe_account", "lba")

    if not email or not fixed_amount:
        return JSONResponse({"ok": False, "error": "email and fixed_amount are required"}, 400)

    # Generate a user_id if not provided
    if not user_id:
        import uuid as _uuid
        user_id = f"collab_{_uuid.uuid4().hex[:12]}"

    now_iso = datetime.now(timezone.utc).isoformat()

    # Check if profile already exists
    existing = (
        sb.table("agent_payout_profiles")
        .select("*")
        .eq("email", email)
        .execute()
    )

    profile_data = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "profile_type": "collaborator",
        "fixed_amount": float(fixed_amount),
        "currency": currency.upper(),
        "payout_frequency": payout_frequency,
        "source_stripe_account": source_stripe_account,
    }

    if existing.data:
        # Update existing profile
        sb.table("agent_payout_profiles").update(profile_data).eq(
            "email", email
        ).execute()
        profile_data["updated"] = True
    else:
        profile_data["created_at"] = now_iso
        sb.table("agent_payout_profiles").insert(profile_data).execute()
        profile_data["created"] = True

    return {"ok": True, "data": profile_data}


@router.put("/admin/collaborators/{user_id}")
async def admin_update_collaborator(user_id: str, request: Request):
    """Update a collaborator's payout config."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    body = await request.json()
    update: dict = {}
    for field in ["name", "email", "fixed_amount", "currency", "payout_frequency", "source_stripe_account"]:
        if field in body:
            update[field] = body[field]

    if "fixed_amount" in update:
        update["fixed_amount"] = float(update["fixed_amount"])
    if "currency" in update:
        update["currency"] = update["currency"].upper()

    if not update:
        return JSONResponse({"ok": False, "error": "No fields to update"}, 400)

    sb.table("agent_payout_profiles").update(update).eq("user_id", user_id).execute()
    return {"ok": True, "message": "Collaborator updated"}


@router.delete("/admin/collaborators/{user_id}")
async def admin_delete_collaborator(user_id: str, request: Request):
    """Remove a collaborator from the payout system."""
    if not await _check_payout_permission(request):
        return JSONResponse({"ok": False, "error": "Payout management permission required"}, 403)

    # Don't hard-delete — just set profile_type back to 'agent' and zero out fixed_amount
    sb.table("agent_payout_profiles").update({
        "profile_type": "agent",
        "fixed_amount": 0,
    }).eq("user_id", user_id).execute()

    return {"ok": True, "message": "Collaborator removed from payouts"}


# ── Commission Protection Settings ────────────────────────────────


@router.get("/admin/commission-settings")
async def get_commission_settings(request: Request):
    """Get platform commission settings (holding period, dispute reserve)."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    settings_resp = sb.table("platform_settings").select("*").in_("key", [
        "commission_holding_days", "commission_dispute_reserve_pct"
    ]).execute()

    result = {}
    for s in settings_resp.data or []:
        result[s["key"]] = s["value"]

    return {"ok": True, "data": result}


@router.put("/admin/commission-settings")
async def update_commission_settings(request: Request):
    """Update platform commission settings."""
    if not _check_admin(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 401)

    body = await request.json()
    for key in ["commission_holding_days", "commission_dispute_reserve_pct"]:
        if key in body:
            sb.table("platform_settings").upsert({
                "key": key,
                "value": str(body[key]),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

    return {"ok": True, "message": "Settings updated"}
