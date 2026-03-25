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
                whop_data = {"name": "Whop", "connected": True, "currency": "USD",
                             "balance": {}, "revenue_30d": 0, "payments_30d": 0}

                # Company info
                r = await client.get("https://api.whop.com/api/v5/company", headers=headers_whop)
                if r.status_code == 200:
                    whop_data["name"] = r.json().get("title", "Whop")

                # Try to get ledger/balance info via v1 API
                try:
                    br = await client.get("https://api.whop.com/api/v1/ledger-accounts",
                                          headers=headers_whop)
                    if br.status_code == 200:
                        ledger_data = br.json()
                        accounts = ledger_data.get("data", ledger_data) if isinstance(ledger_data, dict) else ledger_data
                        if isinstance(accounts, list) and accounts:
                            la = accounts[0]
                            whop_data["balance"] = {
                                "available": la.get("available_balance", la.get("available", 0)),
                                "pending": la.get("pending_balance", la.get("pending", 0)),
                                "reserved": la.get("reserved_balance", la.get("reserved", 0)),
                                "total": la.get("balance", la.get("total", 0)),
                            }
                except Exception:
                    pass  # Ledger endpoint may not be available

                # Get last 30 days of payments for revenue calculation
                thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                total_rev = 0
                total_count = 0
                cursor = None
                for _ in range(10):  # max 10 pages
                    params = {"per": 100, "status": "paid", "created_after": thirty_days_ago}
                    if cursor:
                        params["cursor"] = cursor
                    pr = await client.get(
                        "https://api.whop.com/api/v5/company/payments",
                        params=params, headers=headers_whop,
                    )
                    if pr.status_code == 200:
                        body = pr.json()
                        payments = body.get("data", [])
                        for p in payments:
                            amt = p.get("final_amount", p.get("subtotal", 0)) or 0
                            # Whop amounts may be in cents
                            if amt > 10000:
                                amt = amt / 100
                            total_rev += amt
                            total_count += 1
                        pagination = body.get("pagination", {})
                        if not pagination.get("next_page") and not pagination.get("next_cursor"):
                            break
                        cursor = pagination.get("next_cursor", pagination.get("next_page"))
                        if not cursor:
                            break
                    else:
                        break

                whop_data["revenue_30d"] = round(total_rev, 2)
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
    # Whop balance (available) if we got it, otherwise skip
    whop_balance = result.get("whop", {}).get("balance", {})
    if whop_balance.get("total"):
        total_usd += whop_balance["total"]
    elif whop_balance.get("available"):
        total_usd += whop_balance["available"] + whop_balance.get("pending", 0) + whop_balance.get("reserved", 0)
    result["totals"] = {"usd": total_usd, "mxn": total_mxn}

    return {"ok": True, "data": result}


# ─── Revenue by period ──────────────────────────────────────────────────────


@router.get("/revenue")
async def revenue_by_period(
    request: Request,
    period: str = Query("day", description="day|week|month"),
    days: int = Query(30, description="lookback days"),
    project_id: Optional[str] = None,
    source: Optional[str] = Query(None, description="filter: stripe_uvul, stripe_lba, whop, etc."),
):
    """Revenue from ALL sources (Stripe + Whop), grouped by period."""
    _require_super_admin(request)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_ts = int(since.timestamp())

    revenue = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── Stripe payment_intents (with pagination) ──
        for acct_id, info in STRIPE_ACCOUNTS.items():
            if source and source != f"stripe_{acct_id}":
                continue
            key = _get_stripe_key(acct_id)
            if not key:
                logger.warning("revenue_no_key acct=%s", acct_id)
                continue
            logger.info("revenue_fetch acct=%s since_ts=%s", acct_id, since_ts)
            try:
                starting_after = None
                for _ in range(20):  # max 2000 transactions
                    params = {"created[gte]": since_ts, "limit": 100}
                    if starting_after:
                        params["starting_after"] = starting_after
                    r = await client.get("https://api.stripe.com/v1/payment_intents",
                                         params=params, auth=(key, ""))
                    if r.status_code != 200:
                        logger.warning("stripe_revenue_status acct=%s status=%s", acct_id, r.status_code)
                        break
                    data = r.json()
                    items = data.get("data", [])
                    for pi in items:
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
                            "stripe_id": pi.get("id", ""),
                            "customer_email": pi.get("receipt_email", ""),
                        })
                    if not data.get("has_more") or not items:
                        break
                    starting_after = items[-1]["id"]
                logger.info("revenue_fetched acct=%s items=%s", acct_id, len([r for r in revenue if r.get("source") == f"stripe_{acct_id}"]))
            except Exception as e:
                logger.warning("stripe_revenue_error acct=%s err=%s", acct_id, str(e)[:80])

        # ── Whop payments ──
        whop_key = getattr(settings, "whop_api_key", "")
        if whop_key and (not source or source == "whop"):
            try:
                headers_whop = {"Authorization": f"Bearer {whop_key}"}
                cursor = None
                for _ in range(20):
                    params = {"per": 100, "status": "paid",
                              "created_after": since.isoformat()}
                    if cursor:
                        params["cursor"] = cursor
                    pr = await client.get(
                        "https://api.whop.com/api/v5/company/payments",
                        params=params, headers=headers_whop,
                    )
                    if pr.status_code != 200:
                        break
                    body = pr.json()
                    payments = body.get("data", [])
                    for p in payments:
                        amt = p.get("final_amount", p.get("subtotal", 0)) or 0
                        if amt > 10000:
                            amt = amt / 100
                        raw_date = p.get("created_at", p.get("updated_at", ""))
                        # Whop may return epoch int or ISO string
                        if isinstance(raw_date, (int, float)):
                            created = datetime.fromtimestamp(raw_date, tz=timezone.utc).isoformat()
                        else:
                            created = str(raw_date) if raw_date else ""
                        revenue.append({
                            "source": "whop",
                            "source_name": "Whop",
                            "amount": amt,
                            "currency": (p.get("currency", "usd") or "usd").upper(),
                            "date": created,
                            "campaign_id": "",
                            "lead_id": "",
                            "description": p.get("product_name", p.get("plan_name", "")),
                            "whop_id": p.get("id", ""),
                            "customer_email": p.get("user_email", p.get("email", "")),
                        })
                    pagination = body.get("pagination", {})
                    cursor = pagination.get("next_cursor", pagination.get("next_page"))
                    if not cursor:
                        break
            except Exception as e:
                logger.warning("whop_revenue_error err=%s", str(e)[:80])

    # Sort all revenue by date
    revenue.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Group by period
    grouped = {}
    for item in revenue:
        try:
            dt = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if period == "day":
            pkey = dt.strftime("%Y-%m-%d")
        elif period == "week":
            pkey = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
        else:
            pkey = dt.strftime("%Y-%m")

        if pkey not in grouped:
            grouped[pkey] = {"period": pkey, "total_usd": 0, "total_mxn": 0, "count": 0, "by_source": {}}
        if item["currency"] == "USD":
            grouped[pkey]["total_usd"] += item["amount"]
        elif item["currency"] == "MXN":
            grouped[pkey]["total_mxn"] += item["amount"]
        grouped[pkey]["count"] += 1

        src = item["source"]
        if src not in grouped[pkey]["by_source"]:
            grouped[pkey]["by_source"][src] = {"name": item["source_name"], "amount": 0, "currency": item["currency"]}
        grouped[pkey]["by_source"][src]["amount"] += item["amount"]

    periods = sorted(grouped.values(), key=lambda x: x["period"], reverse=True)

    # Round amounts
    for p in periods:
        p["total_usd"] = round(p["total_usd"], 2)
        p["total_mxn"] = round(p["total_mxn"], 2)
        for s in p["by_source"].values():
            s["amount"] = round(s["amount"], 2)

    return {
        "ok": True,
        "data": {
            "periods": periods,
            "transactions": revenue,
            "summary": {
                "total_usd": round(sum(p["total_usd"] for p in periods), 2),
                "total_mxn": round(sum(p["total_mxn"] for p in periods), 2),
                "total_transactions": len(revenue),
            },
        },
    }


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


# ─── Unified Transactions (all sources as line items) ─────────────────────


@router.get("/transactions")
async def unified_transactions(
    request: Request,
    days: int = Query(30, description="lookback days"),
    source: Optional[str] = Query(None, description="filter: stripe_uvul, mercury_oll, whop, etc."),
    type: Optional[str] = Query(None, description="filter: income|expense|sale"),
    limit: int = Query(200, description="max results"),
):
    """Unified transaction feed — ALL money movements across Stripe, Whop, and Mercury."""
    _require_super_admin(request)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_ts = int(since.timestamp())
    since_str = since.strftime("%Y-%m-%d")
    txns = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── Stripe sales ──
        if not source or source.startswith("stripe"):
            for acct_id, info in STRIPE_ACCOUNTS.items():
                if source and source != f"stripe_{acct_id}":
                    continue
                key = _get_stripe_key(acct_id)
                if not key:
                    continue
                try:
                    starting_after = None
                    for _ in range(10):
                        params = {"created[gte]": since_ts, "limit": 100}
                        if starting_after:
                            params["starting_after"] = starting_after
                        r = await client.get("https://api.stripe.com/v1/payment_intents",
                                             params=params, auth=(key, ""))
                        if r.status_code != 200:
                            break
                        data = r.json()
                        items = data.get("data", [])
                        for pi in items:
                            if pi.get("status") != "succeeded":
                                continue
                            meta = pi.get("metadata") or {}
                            txns.append({
                                "id": pi["id"],
                                "source": f"stripe_{acct_id}",
                                "source_name": info["name"],
                                "type": "sale",
                                "amount": pi["amount"] / 100,
                                "currency": pi["currency"].upper(),
                                "date": datetime.fromtimestamp(pi["created"], tz=timezone.utc).isoformat(),
                                "description": pi.get("description") or f"Stripe payment",
                                "counterparty": pi.get("receipt_email", ""),
                                "campaign_id": meta.get("campaign_id", ""),
                                "lead_id": meta.get("lead_id", ""),
                            })
                        if not data.get("has_more") or not items:
                            break
                        starting_after = items[-1]["id"]
                except Exception as e:
                    logger.warning("txn_stripe_error acct=%s err=%s", acct_id, str(e)[:80])

        # ── Whop sales ──
        whop_key = getattr(settings, "whop_api_key", "")
        if whop_key and (not source or source == "whop"):
            try:
                headers_whop = {"Authorization": f"Bearer {whop_key}"}
                cursor = None
                for _ in range(10):
                    params = {"per": 100, "status": "paid", "created_after": since.isoformat()}
                    if cursor:
                        params["cursor"] = cursor
                    pr = await client.get(
                        "https://api.whop.com/api/v5/company/payments",
                        params=params, headers=headers_whop,
                    )
                    if pr.status_code != 200:
                        break
                    body = pr.json()
                    for p in body.get("data", []):
                        amt = p.get("final_amount", p.get("subtotal", 0)) or 0
                        if amt > 10000:
                            amt = amt / 100
                        raw_d = p.get("created_at", p.get("updated_at", ""))
                        txn_dt = datetime.fromtimestamp(raw_d, tz=timezone.utc).isoformat() if isinstance(raw_d, (int, float)) else str(raw_d or "")
                        txns.append({
                            "id": p.get("id", ""),
                            "source": "whop",
                            "source_name": "Whop",
                            "type": "sale",
                            "amount": amt,
                            "currency": (p.get("currency", "usd") or "usd").upper(),
                            "date": txn_dt,
                            "description": p.get("product_name", p.get("plan_name", "Whop payment")),
                            "counterparty": p.get("user_email", p.get("email", "")),
                            "campaign_id": "",
                            "lead_id": "",
                        })
                    pagination = body.get("pagination", {})
                    cursor = pagination.get("next_cursor", pagination.get("next_page"))
                    if not cursor:
                        break
            except Exception as e:
                logger.warning("txn_whop_error err=%s", str(e)[:80])

        # ── Mercury banking (income + expenses) ──
        if not source or source.startswith("mercury"):
            for acct_id, info in MERCURY_ACCOUNTS.items():
                if source and source != f"mercury_{acct_id}":
                    continue
                key = _get_mercury_key(acct_id)
                if not key:
                    continue
                try:
                    r = await client.get("https://api.mercury.com/api/v1/accounts",
                                         headers={"Authorization": f"Bearer {key}"})
                    if r.status_code != 200:
                        continue
                    merc_accounts = r.json().get("accounts", r.json()) if isinstance(r.json(), dict) else r.json()
                    for ma in merc_accounts:
                        if not isinstance(ma, dict) or not ma.get("id"):
                            continue
                        tr = await client.get(
                            f"https://api.mercury.com/api/v1/account/{ma['id']}/transactions",
                            params={"start": since_str, "limit": 500},
                            headers={"Authorization": f"Bearer {key}"},
                        )
                        if tr.status_code == 200:
                            raw = tr.json().get("transactions", tr.json()) if isinstance(tr.json(), dict) else tr.json()
                            for t in (raw if isinstance(raw, list) else []):
                                amt = t.get("amount", 0)
                                txns.append({
                                    "id": t.get("id", ""),
                                    "source": f"mercury_{acct_id}",
                                    "source_name": info["name"],
                                    "type": "income" if amt > 0 else "expense",
                                    "amount": abs(amt),
                                    "currency": "USD",
                                    "date": t.get("postedDate", t.get("createdAt", "")),
                                    "description": t.get("bankDescription", t.get("note", "")),
                                    "counterparty": t.get("counterpartyName", ""),
                                    "account_name": ma.get("name", ""),
                                    "campaign_id": "",
                                    "lead_id": "",
                                })
                except Exception as e:
                    logger.warning("txn_mercury_error acct=%s err=%s", acct_id, str(e)[:80])

    # Filter by type
    if type:
        txns = [t for t in txns if t["type"] == type]

    # Sort by date descending
    txns.sort(key=lambda x: x.get("date", ""), reverse=True)
    txns = txns[:limit]

    # Summary
    total_sales = round(sum(t["amount"] for t in txns if t["type"] == "sale" and t["currency"] == "USD"), 2)
    total_sales_mxn = round(sum(t["amount"] for t in txns if t["type"] == "sale" and t["currency"] == "MXN"), 2)
    total_income = round(sum(t["amount"] for t in txns if t["type"] == "income"), 2)
    total_expense = round(sum(t["amount"] for t in txns if t["type"] == "expense"), 2)

    return {
        "ok": True,
        "data": {
            "transactions": txns,
            "summary": {
                "sales_usd": total_sales,
                "sales_mxn": total_sales_mxn,
                "mercury_income": total_income,
                "mercury_expense": total_expense,
                "net_usd": total_sales + total_income - total_expense,
            },
            "count": len(txns),
            "sources": list(set(t["source"] for t in txns)),
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


# ═══════════════════════════════════════════════════════════════════════════
# FINANCIAL TRANSACTIONS — Persisted + Project Linking
# ═══════════════════════════════════════════════════════════════════════════


# ─── Sync endpoints ───────────────────────────────────────────────────────


@router.post("/sync")
async def trigger_sync(request: Request):
    """Sync ALL sources (Stripe + Mercury + Whop) → financial_transactions."""
    _require_super_admin(request)
    from ..services.finance_sync import sync_all
    result = await sync_all()
    return {"ok": True, "data": result}


@router.post("/sync/{source}")
async def trigger_sync_source(source: str, request: Request):
    """Sync a single source, e.g. stripe_uvul, mercury_oll, whop."""
    _require_super_admin(request)
    from ..services.finance_sync import sync_stripe, sync_mercury, sync_whop

    if source.startswith("stripe_"):
        acct_id = source.replace("stripe_", "")
        result = await sync_stripe(acct_id)
    elif source.startswith("mercury_"):
        acct_id = source.replace("mercury_", "")
        result = await sync_mercury(acct_id)
    elif source == "whop":
        result = await sync_whop()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")
    return {"ok": True, "data": result}


# ─── Persisted transactions (read from Supabase) ─────────────────────────


@router.get("/stored-transactions")
async def stored_transactions(
    request: Request,
    days: int = Query(30),
    source: Optional[str] = None,
    type: Optional[str] = None,
    project_id: Optional[str] = None,
    unassigned_only: bool = Query(False),
    limit: int = Query(200),
    offset: int = Query(0),
):
    """Read persisted transactions from DB with filters."""
    _require_super_admin(request)

    q = sb.table("financial_transactions").select(
        "*, projects(name)"
    ).order("txn_date", desc=True)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = q.gte("txn_date", since)

    if source:
        q = q.eq("source", source)
    if type:
        q = q.eq("type", type)
    if project_id:
        q = q.eq("project_id", project_id)
    if unassigned_only:
        q = q.is_("project_id", "null")

    q = q.range(offset, offset + limit - 1)
    r = q.execute()
    txns = r.data or []

    # Get counts
    count_q = sb.table("financial_transactions").select("id", count="exact")
    count_q = count_q.gte("txn_date", since)
    if source:
        count_q = count_q.eq("source", source)
    if type:
        count_q = count_q.eq("type", type)
    if project_id:
        count_q = count_q.eq("project_id", project_id)
    if unassigned_only:
        count_q = count_q.is_("project_id", "null")
    count_r = count_q.execute()

    return {
        "ok": True,
        "data": {
            "transactions": txns,
            "count": len(txns),
            "total": count_r.count if hasattr(count_r, "count") else len(txns),
            "offset": offset,
            "limit": limit,
        },
    }


# ─── Transaction assignment ──────────────────────────────────────────────


@router.post("/transactions/{txn_id}/assign")
async def assign_transaction(txn_id: str, request: Request):
    """Manually assign a transaction to a project."""
    _require_super_admin(request)
    body = await request.json()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    r = sb.table("financial_transactions").update({
        "project_id": project_id,
        "auto_assigned": False,
        "updated_at": "now()",
    }).eq("id", txn_id).execute()

    if not r.data:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"ok": True, "data": r.data[0]}


@router.post("/transactions/bulk-assign")
async def bulk_assign_transactions(request: Request):
    """Bulk assign multiple transactions to a project."""
    _require_super_admin(request)
    body = await request.json()
    project_id = body.get("project_id")
    transaction_ids = body.get("transaction_ids", [])
    if not project_id or not transaction_ids:
        raise HTTPException(status_code=400, detail="project_id and transaction_ids required")

    updated = 0
    for tid in transaction_ids:
        try:
            sb.table("financial_transactions").update({
                "project_id": project_id,
                "auto_assigned": False,
                "updated_at": "now()",
            }).eq("id", tid).execute()
            updated += 1
        except Exception:
            pass

    return {"ok": True, "data": {"updated": updated, "total": len(transaction_ids)}}


@router.post("/transactions/apply-rules")
async def apply_rules(request: Request):
    """Re-apply assignment rules to all unassigned transactions."""
    _require_super_admin(request)
    from ..services.finance_sync import apply_rules_to_unassigned
    result = apply_rules_to_unassigned()
    return {"ok": True, "data": result}


# ─── Assignment rules CRUD ───────────────────────────────────────────────


@router.get("/assignment-rules")
async def list_assignment_rules(request: Request):
    _require_super_admin(request)
    r = sb.table("transaction_assignment_rules").select(
        "*, projects(name)"
    ).order("priority", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/assignment-rules")
async def create_assignment_rule(request: Request):
    _require_super_admin(request)
    body = await request.json()
    required = ["project_id", "field", "value"]
    for f in required:
        if not body.get(f):
            raise HTTPException(status_code=400, detail=f"{f} required")

    r = sb.table("transaction_assignment_rules").insert({
        "project_id": body["project_id"],
        "field": body["field"],
        "operator": body.get("operator", "equals"),
        "value": body["value"],
        "priority": body.get("priority", 0),
        "enabled": body.get("enabled", True),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.patch("/assignment-rules/{rule_id}")
async def update_assignment_rule(rule_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    allowed = {"project_id", "field", "operator", "value", "priority", "enabled"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if updates:
        updates["updated_at"] = "now()"
        r = sb.table("transaction_assignment_rules").update(updates).eq("id", rule_id).execute()
        return {"ok": True, "data": (r.data or [{}])[0]}
    return {"ok": True}


@router.delete("/assignment-rules/{rule_id}")
async def delete_assignment_rule(rule_id: str, request: Request):
    _require_super_admin(request)
    sb.table("transaction_assignment_rules").delete().eq("id", rule_id).execute()
    return {"ok": True}


# ─── Profitability per project ───────────────────────────────────────────


@router.get("/profitability")
async def project_profitability(
    request: Request,
    days: int = Query(30),
    project_id: Optional[str] = None,
):
    """Per-project profitability: revenue - expenses."""
    _require_super_admin(request)

    # Try RPC first (faster), fall back to Python aggregation
    try:
        rpc_result = sb.rpc("fn_project_profitability", {"p_days": days}).execute()
        projects_data = rpc_result.data or []
    except Exception:
        # Fallback: aggregate in Python
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = sb.table("financial_transactions").select(
            "project_id,type,amount,currency"
        ).gte("txn_date", since).not_.is_("project_id", "null").neq("type", "transfer")
        if project_id:
            q = q.eq("project_id", project_id)
        r = q.execute()

        agg = {}
        for t in (r.data or []):
            pid = t["project_id"]
            if pid not in agg:
                agg[pid] = {"project_id": pid, "revenue_usd": 0, "revenue_mxn": 0,
                            "expenses_usd": 0, "expenses_mxn": 0, "refunds_usd": 0,
                            "transaction_count": 0}
            a = agg[pid]
            a["transaction_count"] += 1
            if t["type"] in ("sale", "income"):
                if t["currency"] == "USD":
                    a["revenue_usd"] += float(t["amount"])
                elif t["currency"] == "MXN":
                    a["revenue_mxn"] += float(t["amount"])
            elif t["type"] == "expense":
                if t["currency"] == "USD":
                    a["expenses_usd"] += float(t["amount"])
                elif t["currency"] == "MXN":
                    a["expenses_mxn"] += float(t["amount"])
            elif t["type"] == "refund":
                a["refunds_usd"] += float(t["amount"])

        # Get project names
        proj_r = sb.table("projects").select("id,name").execute()
        proj_names = {p["id"]: p["name"] for p in (proj_r.data or [])}
        for v in agg.values():
            v["project_name"] = proj_names.get(v["project_id"], "Unknown")

        projects_data = sorted(agg.values(), key=lambda x: x["revenue_usd"], reverse=True)

    # Filter if specific project requested
    if project_id:
        projects_data = [p for p in projects_data if p.get("project_id") == project_id]

    # Calculate profit + margin for each
    for p in projects_data:
        p["revenue_usd"] = round(float(p.get("revenue_usd", 0)), 2)
        p["revenue_mxn"] = round(float(p.get("revenue_mxn", 0)), 2)
        p["expenses_usd"] = round(float(p.get("expenses_usd", 0)), 2)
        p["expenses_mxn"] = round(float(p.get("expenses_mxn", 0)), 2)
        p["refunds_usd"] = round(float(p.get("refunds_usd", 0)), 2)
        p["profit_usd"] = round(p["revenue_usd"] - p["expenses_usd"] - p["refunds_usd"], 2)
        p["margin_pct"] = round(
            p["profit_usd"] / p["revenue_usd"] * 100, 1
        ) if p["revenue_usd"] > 0 else 0

    # Totals
    totals = {
        "revenue_usd": round(sum(p["revenue_usd"] for p in projects_data), 2),
        "revenue_mxn": round(sum(p["revenue_mxn"] for p in projects_data), 2),
        "expenses_usd": round(sum(p["expenses_usd"] for p in projects_data), 2),
        "refunds_usd": round(sum(p["refunds_usd"] for p in projects_data), 2),
        "profit_usd": round(sum(p["profit_usd"] for p in projects_data), 2),
    }

    # Count unassigned
    try:
        unassigned_r = sb.table("financial_transactions").select(
            "id", count="exact"
        ).is_("project_id", "null").execute()
        unassigned_count = unassigned_r.count if hasattr(unassigned_r, "count") else 0
    except Exception:
        unassigned_count = 0

    return {
        "ok": True,
        "data": {
            "projects": projects_data,
            "totals": totals,
            "unassigned_count": unassigned_count,
            "period_days": days,
        },
    }
