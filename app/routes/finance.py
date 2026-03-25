"""Financial dashboard endpoints — Super Admin only."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("finance")
router = APIRouter(prefix="/v1/finance", tags=["finance"])

# ─── Stripe account map ────────────────────────────────────────────────────

STRIPE_ACCOUNTS = {
    "uvul": {"name": "Una Vida Un Legado (MX)", "currency": "MXN"},
    "lba": {"name": "Legacy Business Academy", "currency": "USD"},
    "oll": {"name": "One Life Legacy", "currency": "USD"},
    "2clicks": {"name": "2clicks.com", "currency": "USD"},
}

MERCURY_ACCOUNTS = {
    "oll": {"name": "One Life Legacy"},
    "2clicks": {"name": "2clicks.com"},
    "lba": {"name": "Legacy Business Academy"},
}


def _get_stripe_key(account: str) -> str:
    key_map = {
        "uvul": getattr(settings, "stripe_key_uvul", ""),
        "lba": getattr(settings, "stripe_key_lba", ""),
        "oll": getattr(settings, "stripe_key_oll", ""),
        "2clicks": getattr(settings, "stripe_key_2clicks", ""),
    }
    return key_map.get(account, "")


def _get_mercury_key(account: str) -> str:
    key_map = {
        "oll": getattr(settings, "mercury_key_oll", ""),
        "2clicks": getattr(settings, "mercury_key_2clicks", ""),
        "lba": getattr(settings, "mercury_key_lba", ""),
    }
    return key_map.get(account, "")


def _require_super_admin(request: Request):
    """Only super admin (Spencer) can access global finance data."""
    token = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    spartans_key = (request.headers.get("x-spartans-key") or "").strip()
    cron_token = (request.headers.get("x-cron-token") or "").strip()

    if settings.cron_token and cron_token == settings.cron_token:
        return
    if spartans_key and settings.spartans_key and spartans_key == settings.spartans_key:
        return

    # Check JWT from Supabase auth — verify user is org owner
    if token:
        try:
            import jwt as pyjwt
            decoded = pyjwt.decode(token, options={"verify_signature": False})
            uid = decoded.get("sub", "")
            if uid:
                member = sb.table("org_members").select("role").eq("user_id", uid).eq("role", "owner").limit(1).execute()
                if member.data:
                    logger.info("finance_auth_ok user=%s role=owner", uid)
                    return
                logger.warning("finance_auth_denied user=%s no_owner_role", uid)
        except Exception as e:
            logger.warning("finance_auth_jwt_error err=%s", str(e)[:80])

    # Dev mode fallback
    if not settings.cron_token:
        return

    raise HTTPException(status_code=403, detail="Super admin access required")


# ─── Projects CRUD ──────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects(request: Request):
    _require_super_admin(request)
    r = sb.table("projects").select("*").order("created_at", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/projects")
async def create_project(request: Request):
    _require_super_admin(request)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    r = sb.table("projects").insert({
        "name": name,
        "description": body.get("description", ""),
        "stripe_account": body.get("stripe_account", ""),
        "mercury_account": body.get("mercury_account", ""),
        "config": body.get("config", {}),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    allowed = {"name", "description", "stripe_account", "mercury_account", "status", "config"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if updates:
        updates["updated_at"] = "now()"
        r = sb.table("projects").update(updates).eq("id", project_id).execute()
        return {"ok": True, "data": (r.data or [{}])[0]}
    return {"ok": True}


# ─── Global Financial Overview ──────────────────────────────────────────────


@router.get("/overview")
async def financial_overview(request: Request):
    """Global financial overview — all Stripe + Mercury + Whop balances."""
    _require_super_admin(request)

    result = {"stripe": {}, "mercury": {}, "whop": None, "totals": {}}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Stripe balances
        for acct_id, info in STRIPE_ACCOUNTS.items():
            key = _get_stripe_key(acct_id)
            if not key:
                continue
            try:
                r = await client.get("https://api.stripe.com/v1/balance", auth=(key, ""))
                if r.status_code == 200:
                    d = r.json()
                    available = sum(b["amount"] for b in d.get("available", [])) / 100
                    pending = sum(b["amount"] for b in d.get("pending", [])) / 100
                    currency = d["available"][0]["currency"].upper() if d.get("available") else info["currency"]
                    result["stripe"][acct_id] = {
                        "name": info["name"],
                        "available": available,
                        "pending": pending,
                        "currency": currency,
                    }
            except Exception as e:
                logger.warning("stripe_balance_error acct=%s err=%s", acct_id, str(e)[:80])

        # Mercury balances
        for acct_id, info in MERCURY_ACCOUNTS.items():
            key = _get_mercury_key(acct_id)
            if not key:
                continue
            try:
                r = await client.get("https://api.mercury.com/api/v1/accounts",
                                     headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    accounts = r.json().get("accounts", r.json()) if isinstance(r.json(), dict) else r.json()
                    total = sum(a.get("currentBalance", 0) for a in accounts if isinstance(a, dict))
                    result["mercury"][acct_id] = {
                        "name": info["name"],
                        "balance": total,
                        "currency": "USD",
                        "accounts": [{"name": a.get("name", ""), "balance": a.get("currentBalance", 0)}
                                     for a in accounts if isinstance(a, dict)],
                    }
            except Exception as e:
                logger.warning("mercury_balance_error acct=%s err=%s", acct_id, str(e)[:80])

        # Whop — get company info + balance + recent revenue
        whop_key = getattr(settings, "whop_api_key", "")
        if whop_key:
            try:
                headers_whop = {"Authorization": f"Bearer {whop_key}"}
                whop_data: dict = {"name": "Whop", "connected": True, "balance": None, "revenue_30d": 0, "payments_30d": 0, "currency": "USD"}

                # Company info + get company ID for balance
                company_id = None
                r = await client.get("https://api.whop.com/api/v5/company", headers=headers_whop)
                if r.status_code == 200:
                    company_data = r.json()
                    whop_data["name"] = company_data.get("title", "Whop")
                    company_id = company_data.get("id", "")

                # Fetch balance via ledger_accounts endpoint (correct API)
                if company_id:
                    try:
                        ledger_url = f"https://api.whop.com/api/v1/ledger_accounts/{company_id}"
                        br = await client.get(ledger_url, headers=headers_whop)
                        logger.info("whop_balance url=%s status=%s", ledger_url, br.status_code)
                        if br.status_code == 200:
                            bdata = br.json()
                            for bal_entry in bdata.get("balances", []):
                                curr = (bal_entry.get("currency", "usd") or "usd").lower()
                                if curr == "usd":
                                    available_bal = bal_entry.get("balance", 0) or 0
                                    pending_bal = bal_entry.get("pending_balance", 0) or 0
                                    reserve_bal = bal_entry.get("reserve_balance", 0) or 0
                                    if available_bal > 1000000:
                                        available_bal /= 100
                                        pending_bal /= 100
                                        reserve_bal /= 100
                                    total_bal = available_bal + pending_bal + reserve_bal
                                    whop_data["balance"] = {
                                        "available": round(available_bal, 2),
                                        "pending": round(pending_bal, 2),
                                        "reserved": round(reserve_bal, 2),
                                        "total": round(total_bal, 2),
                                    }
                                    break
                    except Exception as e:
                        logger.warning("whop_balance_error err=%s", str(e)[:100])

                # Get recent payments to calculate revenue (last 500)
                total_rev = 0
                total_count = 0
                for page in range(1, 6):
                    pr = await client.get(
                        f"https://api.whop.com/api/v5/company/payments?per=100&page={page}&status=paid",
                        headers=headers_whop,
                    )
                    if pr.status_code == 200:
                        payments = pr.json().get("data", [])
                        for p in payments:
                            total_rev += p.get("subtotal", 0) or 0
                            total_count += 1
                        if not pr.json().get("pagination", {}).get("next_page"):
                            break
                    else:
                        break

                whop_data["revenue_30d"] = total_rev
                whop_data["payments_30d"] = total_count
                result["whop"] = whop_data
            except Exception as e:
                logger.warning("whop_error err=%s", str(e)[:80])

    # Calculate totals
    total_usd = 0
    total_mxn = 0
    for acct in result["stripe"].values():
        if acct["currency"] == "USD":
            total_usd += acct["available"] + acct["pending"]
        elif acct["currency"] == "MXN":
            total_mxn += acct["available"] + acct["pending"]
    for acct in result["mercury"].values():
        total_usd += acct["balance"]
    whop_balance = (result.get("whop") or {}).get("balance") or {}
    if whop_balance.get("total"):
        total_usd += whop_balance["total"]
    result["totals"] = {"usd": total_usd, "mxn": total_mxn}

    return {"ok": True, "data": result}


# ─── Revenue by period ──────────────────────────────────────────────────────


@router.get("/revenue")
async def revenue_by_period(
    request: Request,
    period: str = Query("day", description="day|week|month"),
    days: int = Query(30, description="lookback days"),
    project_id: Optional[str] = None,
):
    """Revenue from Stripe charges, grouped by period."""
    _require_super_admin(request)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_ts = int(since.timestamp())

    revenue = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for acct_id, info in STRIPE_ACCOUNTS.items():
            key = _get_stripe_key(acct_id)
            if not key:
                continue
            try:
                # Get payment_intents from Stripe (more reliable than charges)
                params = {"created[gte]": since_ts, "limit": 100}
                r = await client.get("https://api.stripe.com/v1/payment_intents",
                                     params=params, auth=(key, ""))
                if r.status_code == 200:
                    for pi in r.json().get("data", []):
                        if pi.get("status") != "succeeded":
                            continue
                        meta = pi.get("metadata") or {}
                        revenue.append({
                            "source": f"stripe_{acct_id}",
                            "source_name": info["name"],
                            "amount": pi["amount"] / 100,
                            "currency": pi["currency"].upper(),
                            "date": datetime.fromtimestamp(pi["created"], tz=timezone.utc).isoformat(),
                            "campaign_id": meta.get("campaign_id", ""),
                            "lead_id": meta.get("lead_id", ""),
                            "description": pi.get("description", ""),
                        })
                else:
                    logger.warning("stripe_revenue_status acct=%s status=%s", acct_id, r.status_code)
            except Exception as e:
                logger.warning("stripe_revenue_error acct=%s err=%s", acct_id, str(e)[:80])

        # Whop payments — mapped to Legacy Business Academy
        whop_key = getattr(settings, "whop_api_key", "")
        logger.info("whop_revenue_check key_exists=%s", bool(whop_key))
        if whop_key:
            try:
                headers_whop = {"Authorization": f"Bearer {whop_key}"}
                whop_count = 0
                whop_skipped = 0
                for page in range(1, 10):  # up to 900 payments
                    pr = await client.get(
                        f"https://api.whop.com/api/v5/company/payments?per=100&page={page}&status=paid",
                        headers=headers_whop,
                    )
                    logger.info("whop_revenue_page page=%d status=%d", page, pr.status_code)
                    if pr.status_code != 200:
                        logger.warning("whop_revenue_page_error body=%s", str(pr.text)[:200])
                        break
                    payments = pr.json().get("data", [])
                    logger.info("whop_revenue_payments_count page=%d count=%d", page, len(payments))
                    past_window = False
                    for p in payments:
                        created = p.get("created_at", p.get("updated_at", ""))
                        if not created:
                            continue
                        try:
                            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        except Exception:
                            continue
                        if dt < since:
                            past_window = True
                            whop_skipped += 1
                            continue  # skip old ones, don't break (order may not be strict)
                        subtotal = p.get("subtotal", 0) or 0
                        # Whop subtotal is in dollars (not cents)
                        if subtotal > 0:
                            revenue.append({
                                "source": "stripe_lba",  # Whop = Legacy Business Academy
                                "source_name": "Legacy Business Academy",
                                "amount": subtotal,
                                "currency": "USD",
                                "date": dt.isoformat(),
                                "campaign_id": "",
                                "lead_id": "",
                                "description": f"Whop: {p.get('product_name', p.get('plan_id', 'Payment'))}",
                            })
                            whop_count += 1
                    if past_window and whop_skipped > 10:
                        break  # We're clearly past our date window
                    if not pr.json().get("pagination", {}).get("next_page"):
                        break
                logger.info("whop_revenue_done added=%d skipped=%d", whop_count, whop_skipped)
            except Exception as e:
                logger.warning("whop_revenue_error err=%s", str(e)[:100])

    # Group by period
    grouped = {}
    for item in revenue:
        dt = datetime.fromisoformat(item["date"])
        if period == "day":
            key = dt.strftime("%Y-%m-%d")
        elif period == "week":
            key = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
        else:
            key = dt.strftime("%Y-%m")

        if key not in grouped:
            grouped[key] = {"period": key, "total_usd": 0, "total_mxn": 0, "count": 0, "by_source": {}}
        if item["currency"] == "USD":
            grouped[key]["total_usd"] += item["amount"]
        elif item["currency"] == "MXN":
            grouped[key]["total_mxn"] += item["amount"]
        grouped[key]["count"] += 1

        # Key by source + currency so MXN and USD from same source stay separate
        src = f"{item['source']}_{item['currency']}"
        if src not in grouped[key]["by_source"]:
            grouped[key]["by_source"][src] = {"name": item["source_name"], "amount": 0, "currency": item["currency"]}
        grouped[key]["by_source"][src]["amount"] += item["amount"]

    periods = sorted(grouped.values(), key=lambda x: x["period"], reverse=True)
    total_usd = sum(t["amount"] for t in revenue if t["currency"] == "USD")
    total_mxn = sum(t["amount"] for t in revenue if t["currency"] == "MXN")
    return {"ok": True, "data": {
        "periods": periods,
        "transactions": revenue,
        "summary": {"total_usd": total_usd, "total_mxn": total_mxn, "count": len(revenue)},
    }}


# ─── Mercury transactions ──────────────────────────────────────────────────


@router.get("/expenses")
async def expenses(
    request: Request,
    days: int = Query(30),
    account: Optional[str] = None,
):
    """Mercury transactions (expenses/income) for cost tracking."""
    _require_super_admin(request)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    transactions = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for acct_id, info in MERCURY_ACCOUNTS.items():
            if account and acct_id != account:
                continue
            key = _get_mercury_key(acct_id)
            if not key:
                continue
            try:
                # Get Mercury accounts first
                r = await client.get("https://api.mercury.com/api/v1/accounts",
                                     headers={"Authorization": f"Bearer {key}"})
                if r.status_code != 200:
                    continue
                merc_accounts = r.json().get("accounts", r.json()) if isinstance(r.json(), dict) else r.json()

                for ma in merc_accounts:
                    if not isinstance(ma, dict):
                        continue
                    ma_id = ma.get("id", "")
                    if not ma_id:
                        continue
                    # Get transactions
                    tr = await client.get(
                        f"https://api.mercury.com/api/v1/account/{ma_id}/transactions",
                        params={"start": since, "limit": 500},
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    if tr.status_code == 200:
                        txns = tr.json().get("transactions", tr.json()) if isinstance(tr.json(), dict) else tr.json()
                        for t in (txns if isinstance(txns, list) else []):
                            transactions.append({
                                "source": f"mercury_{acct_id}",
                                "source_name": info["name"],
                                "account_name": ma.get("name", ""),
                                "amount": t.get("amount", 0),
                                "date": t.get("postedDate", t.get("createdAt", "")),
                                "description": t.get("bankDescription", t.get("note", "")),
                                "counterparty": t.get("counterpartyName", ""),
                                "type": "income" if t.get("amount", 0) > 0 else "expense",
                                "category": t.get("details", {}).get("category", ""),
                            })
            except Exception as e:
                logger.warning("mercury_txn_error acct=%s err=%s", acct_id, str(e)[:80])

    # Sort by date
    transactions.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Summary
    total_income = sum(t["amount"] for t in transactions if t["amount"] > 0)
    total_expense = sum(abs(t["amount"]) for t in transactions if t["amount"] < 0)

    return {
        "ok": True,
        "data": {
            "transactions": transactions,
            "summary": {
                "total_income": total_income,
                "total_expense": total_expense,
                "net": total_income - total_expense,
            },
        },
    }


# ─── Commission config per profile ─────────────────────────────────────────


@router.get("/commission-rules")
async def get_commission_rules(request: Request, campaign_id: str = Query(...)):
    """Get all commission rules for a campaign, grouped by profile."""
    _require_super_admin(request)
    configs = sb.table("commission_configs").select("*").eq("campaign_id", campaign_id).execute()
    tiers_by_config = {}
    for cfg in (configs.data or []):
        t = sb.table("commission_tiers").select("*").eq("config_id", cfg["id"]).order("min_sales").execute()
        tiers_by_config[cfg["id"]] = t.data or []
    result = []
    for cfg in (configs.data or []):
        result.append({**cfg, "tiers": tiers_by_config.get(cfg["id"], [])})
    return {"ok": True, "data": result}


@router.post("/commission-rules")
async def upsert_commission_rule(request: Request):
    """Create or update a commission rule for a campaign + profile + tier."""
    _require_super_admin(request)
    body = await request.json()
    campaign_id = body.get("campaign_id", "")
    profile_type = body.get("profile_type", "confirmador")
    tier = body.get("tier", "VIP")
    commission_type = body.get("commission_type", "percentage")
    commission_value = body.get("commission_value", 0)

    if not campaign_id:
        raise HTTPException(status_code=400, detail="campaign_id required")

    # Check if exists
    existing = (
        sb.table("commission_configs")
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("profile_type", profile_type)
        .eq("tier", tier)
        .limit(1)
        .execute()
    )

    if existing.data:
        r = sb.table("commission_configs").update({
            "commission_type": commission_type,
            "commission_value": commission_value,
            "updated_at": "now()",
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        r = sb.table("commission_configs").insert({
            "campaign_id": campaign_id,
            "profile_type": profile_type,
            "tier": tier,
            "commission_type": commission_type,
            "commission_value": commission_value,
        }).execute()

    return {"ok": True, "data": (r.data or [{}])[0]}
