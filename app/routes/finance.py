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
    allowed = {"name", "description", "stripe_account", "mercury_account", "status", "config", "leader_id", "leader_name"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if updates:
        updates["updated_at"] = "now()"
        r = sb.table("projects").update(updates).eq("id", project_id).execute()
        return {"ok": True, "data": (r.data or [{}])[0]}
    return {"ok": True}


# ─── Project Clients CRUD ──────────────────────────────────────────────────


@router.get("/projects/{project_id}/clients")
async def list_project_clients(project_id: str, request: Request, status: Optional[str] = None):
    _require_super_admin(request)
    q = sb.table("project_clients").select("*").eq("project_id", project_id).order("created_at", desc=True)
    if status:
        q = q.eq("status", status)
    r = q.execute()
    return {"ok": True, "data": r.data or []}


@router.post("/projects/{project_id}/clients")
async def create_project_client(project_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    if not body.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    r = sb.table("project_clients").insert({
        "project_id": project_id,
        "name": body["name"],
        "email": body.get("email", ""),
        "phone": body.get("phone", ""),
        "status": body.get("status", "active"),
        "total_amount": body.get("total_amount", 0),
        "paid_amount": body.get("paid_amount", 0),
        "currency": body.get("currency", "USD"),
        "payment_plan": body.get("payment_plan", {}),
        "notes": body.get("notes", ""),
        "lead_id": body.get("lead_id", ""),
        "stripe_customer_id": body.get("stripe_customer_id", ""),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.patch("/clients/{client_id}")
async def update_project_client(client_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    allowed = {"name", "email", "phone", "status", "total_amount", "paid_amount",
               "currency", "payment_plan", "notes", "metadata"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        r = sb.table("project_clients").update(updates).eq("id", client_id).execute()
        return {"ok": True, "data": (r.data or [{}])[0]}
    return {"ok": True}


# ─── Client Payments ──────────────────────────────────────────────────────


@router.get("/clients/{client_id}/payments")
async def list_client_payments(client_id: str, request: Request):
    _require_super_admin(request)
    r = sb.table("client_payments").select("*").eq("client_id", client_id).order("txn_date", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/clients/{client_id}/payments")
async def create_client_payment(client_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    # Get client to find project_id
    client = sb.table("project_clients").select("project_id,paid_amount").eq("id", client_id).limit(1).execute()
    if not client.data:
        raise HTTPException(status_code=404, detail="Client not found")
    project_id = client.data[0]["project_id"]
    amount = body.get("amount", 0)

    r = sb.table("client_payments").insert({
        "client_id": client_id,
        "project_id": project_id,
        "amount": amount,
        "currency": body.get("currency", "USD"),
        "payment_method": body.get("payment_method", "stripe"),
        "status": body.get("status", "completed"),
        "txn_date": body.get("txn_date", datetime.now(timezone.utc).isoformat()),
        "external_id": body.get("external_id", ""),
        "description": body.get("description", ""),
    }).execute()

    # Update client paid_amount
    if body.get("status", "completed") == "completed":
        new_paid = (client.data[0].get("paid_amount") or 0) + amount
        sb.table("project_clients").update({
            "paid_amount": new_paid,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", client_id).execute()

    return {"ok": True, "data": (r.data or [{}])[0]}


# ─── Manual Transactions (Zelle, Cash, Wire) ──────────────────────────────


@router.get("/manual-transactions")
async def list_manual_transactions(request: Request, project_id: Optional[str] = None, days: int = Query(30)):
    _require_super_admin(request)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = sb.table("manual_transactions").select("*, projects(name)").gte("txn_date", since).order("txn_date", desc=True)
    if project_id:
        q = q.eq("project_id", project_id)
    r = q.execute()
    return {"ok": True, "data": r.data or []}


@router.post("/manual-transactions")
async def create_manual_transaction(request: Request):
    _require_super_admin(request)
    body = await request.json()
    required = ["project_id", "type", "amount", "payment_method"]
    for f in required:
        if not body.get(f):
            raise HTTPException(status_code=400, detail=f"{f} is required")
    r = sb.table("manual_transactions").insert({
        "project_id": body["project_id"],
        "client_id": body.get("client_id"),
        "type": body["type"],
        "amount": body["amount"],
        "currency": body.get("currency", "USD"),
        "payment_method": body["payment_method"],
        "counterparty": body.get("counterparty", ""),
        "description": body.get("description", ""),
        "txn_date": body.get("txn_date", datetime.now(timezone.utc).isoformat()),
        "receipt_url": body.get("receipt_url", ""),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


# ─── Project Expense Sources (Mercury cards) ──────────────────────────────


@router.get("/projects/{project_id}/expense-sources")
async def list_expense_sources(project_id: str, request: Request):
    _require_super_admin(request)
    r = sb.table("project_expense_sources").select("*").eq("project_id", project_id).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/projects/{project_id}/expense-sources")
async def create_expense_source(project_id: str, request: Request):
    _require_super_admin(request)
    body = await request.json()
    r = sb.table("project_expense_sources").insert({
        "project_id": project_id,
        "source_type": body.get("source_type", "mercury_card"),
        "identifier": body.get("identifier", ""),
        "label": body.get("label", ""),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/expense-sources/{source_id}")
async def delete_expense_source(source_id: str, request: Request):
    _require_super_admin(request)
    sb.table("project_expense_sources").delete().eq("id", source_id).execute()
    return {"ok": True}


# ─── Campaign–Project Linking ──────────────────────────────────────────────


@router.get("/projects/{project_id}/campaigns")
async def list_project_campaigns(project_id: str, request: Request):
    """List campaigns linked to a project."""
    _require_super_admin(request)
    r = (
        sb.table("campaigns")
        .select("id, name, status, event_name, event_date, created_at, project_id")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"ok": True, "data": r.data or []}


@router.post("/projects/{project_id}/campaigns/{campaign_id}/link")
async def link_campaign_to_project(project_id: str, campaign_id: str, request: Request):
    """Link a campaign to a project."""
    _require_super_admin(request)
    r = sb.table("campaigns").update({"project_id": project_id}).eq("id", campaign_id).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/projects/{project_id}/campaigns/{campaign_id}/unlink")
async def unlink_campaign_from_project(project_id: str, campaign_id: str, request: Request):
    """Unlink a campaign from a project."""
    _require_super_admin(request)
    r = sb.table("campaigns").update({"project_id": None}).eq("id", campaign_id).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.get("/unlinked-campaigns")
async def list_unlinked_campaigns(request: Request):
    """List campaigns that are not linked to any project."""
    _require_super_admin(request)
    r = (
        sb.table("campaigns")
        .select("id, name, status, event_name, created_at")
        .is_("project_id", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return {"ok": True, "data": r.data or []}


# ─── Project Agents CRUD ──────────────────────────────────────────────────


@router.get("/projects/{project_id}/agents")
async def list_project_agents(project_id: str, request: Request):
    """List agents assigned to a project."""
    _require_super_admin(request)
    r = sb.table("project_agents").select("*").eq("project_id", project_id).order("created_at", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/projects/{project_id}/agents")
async def create_project_agent(project_id: str, request: Request):
    """Add an agent to a project."""
    _require_super_admin(request)
    body = await request.json()
    if not body.get("user_id"):
        raise HTTPException(status_code=400, detail="user_id is required")
    r = sb.table("project_agents").insert({
        "project_id": project_id,
        "user_id": body["user_id"],
        "role": body.get("role", "agent"),
        "commission_rate": body.get("commission_rate", 0),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/agents/{agent_id}")
async def delete_project_agent(agent_id: str, request: Request):
    """Remove an agent from a project."""
    _require_super_admin(request)
    sb.table("project_agents").delete().eq("id", agent_id).execute()
    return {"ok": True}


# ─── Project Payment Gateways CRUD ────────────────────────────────────────


@router.get("/projects/{project_id}/payment-gateways")
async def list_project_payment_gateways(project_id: str, request: Request):
    """List payment gateways for a project."""
    _require_super_admin(request)
    r = sb.table("project_payment_gateways").select("*").eq("project_id", project_id).order("created_at", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/projects/{project_id}/payment-gateways")
async def create_project_payment_gateway(project_id: str, request: Request):
    """Add a payment gateway to a project."""
    _require_super_admin(request)
    body = await request.json()
    if not body.get("gateway_type"):
        raise HTTPException(status_code=400, detail="gateway_type is required")
    r = sb.table("project_payment_gateways").insert({
        "project_id": project_id,
        "gateway_type": body["gateway_type"],
        "gateway_key": body.get("gateway_key", ""),
        "label": body.get("label", ""),
        "is_primary": body.get("is_primary", False),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/payment-gateways/{gateway_id}")
async def delete_project_payment_gateway(gateway_id: str, request: Request):
    """Remove a payment gateway from a project."""
    _require_super_admin(request)
    sb.table("project_payment_gateways").delete().eq("id", gateway_id).execute()
    return {"ok": True}


# ─── Assignment Rules CRUD ──────────────────────────────────────────────────


@router.get("/assignment-rules")
async def list_assignment_rules(request: Request, project_id: Optional[str] = None):
    _require_super_admin(request)
    q = sb.table("transaction_assignment_rules").select("*").order("priority", desc=True)
    if project_id:
        q = q.eq("project_id", project_id)
    r = q.execute()
    return {"ok": True, "data": r.data or []}


@router.post("/assignment-rules")
async def create_assignment_rule(request: Request):
    _require_super_admin(request)
    body = await request.json()
    required = ["project_id", "field", "value"]
    for f in required:
        if not body.get(f):
            raise HTTPException(status_code=400, detail=f"{f} is required")
    r = sb.table("transaction_assignment_rules").insert({
        "project_id": body["project_id"],
        "field": body["field"],
        "operator": body.get("operator", "equals"),
        "value": body["value"],
        "priority": body.get("priority", 0),
        "enabled": body.get("enabled", True),
    }).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/assignment-rules/{rule_id}")
async def delete_assignment_rule(rule_id: str, request: Request):
    _require_super_admin(request)
    sb.table("transaction_assignment_rules").delete().eq("id", rule_id).execute()
    return {"ok": True}


# ─── Bulk Assign Transactions ──────────────────────────────────────────────


@router.post("/bulk-assign")
async def bulk_assign_transactions(request: Request):
    """
    Bulk-assign transactions to a project by criteria.
    Body: {
        project_id: str (required),
        amounts: [79, 97],           # match ANY of these amounts
        sources: ["stripe_lba"],     # match ANY of these sources (optional, all if empty)
        currencies: ["USD"],         # match currency (optional, default USD)
        date_from: "2026-02-26",     # start date (optional)
        date_to: "2026-03-15",       # end date (optional)
        description_contains: "VIP", # description filter (optional)
        dry_run: false               # if true, just return matches without assigning
    }
    """
    _require_super_admin(request)
    body = await request.json()

    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")

    amounts = body.get("amounts", [])
    sources = body.get("sources", [])
    currencies = body.get("currencies", ["USD"])
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    description_contains = body.get("description_contains", "")
    dry_run = body.get("dry_run", False)

    # Search across live revenue data from Stripe + Whop
    # Then create/update financial_transactions with project_id
    matched = []

    async with httpx.AsyncClient(timeout=30) as client:
        stripe_keys = {
            "uvul": _get_stripe_key("uvul"),
            "lba": _get_stripe_key("lba"),
            "oll": _get_stripe_key("oll"),
            "2clicks": _get_stripe_key("2clicks"),
        }
        for label, key in stripe_keys.items():
            if not key:
                continue
            source = f"stripe_{label}"
            if sources and source not in sources:
                continue

            params = {"limit": 100}
            if date_from:
                params["created[gte]"] = int(datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc).timestamp())
            if date_to:
                params["created[lte]"] = int(datetime.fromisoformat(date_to + "T23:59:59").replace(tzinfo=timezone.utc).timestamp())

            cursor = None
            for _ in range(20):  # max 20 pages
                if cursor:
                    params["starting_after"] = cursor
                r = await client.get(
                    "https://api.stripe.com/v1/payment_intents",
                    params=params, auth=(key, ""),
                )
                if r.status_code != 200:
                    logger.warning("bulk_assign stripe_error source=%s status=%s body=%s", source, r.status_code, r.text[:200])
                    break
                data = r.json()
                page_items = data.get("data", [])
                logger.info("bulk_assign page source=%s items=%d", source, len(page_items))
                for pi in page_items:
                    # Only succeeded payments
                    if pi.get("status") != "succeeded":
                        continue
                    amt = pi["amount"] / 100
                    curr = (pi.get("currency") or "usd").upper()

                    # Filter by amounts (use rounding for float comparison)
                    if amounts and round(amt, 2) not in [round(float(a), 2) for a in amounts]:
                        continue
                    # Filter by currency
                    if currencies and curr not in currencies:
                        continue
                    # Filter by description
                    desc = pi.get("description") or ""
                    if description_contains and description_contains.lower() not in desc.lower():
                        pass  # Don't filter by description for Stripe (often empty)

                    dt = datetime.fromtimestamp(pi["created"], tz=timezone.utc)
                    matched.append({
                        "external_id": pi["id"],
                        "source": source,
                        "type": "sale",
                        "amount": amt,
                        "currency": curr,
                        "txn_date": dt.isoformat(),
                        "description": desc or f"Stripe payment ${amt} {curr}",
                        "counterparty": pi.get("receipt_email") or "",
                        "project_id": project_id,
                        "auto_assigned": True,
                        "metadata": {"bulk_assigned": True, "criteria": {
                            "amounts": amounts, "sources": sources,
                            "date_from": date_from, "date_to": date_to,
                        }},
                    })
                    cursor = pi["id"]

                if not data.get("has_more"):
                    break

    logger.info("bulk_assign_result matched=%d total=%.2f dry_run=%s",
                len(matched), sum(t["amount"] for t in matched), dry_run)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "matched_count": len(matched),
            "total_amount": sum(t["amount"] for t in matched),
            "sample": matched[:10],
        }

    # Upsert matched transactions into financial_transactions
    assigned = 0
    for txn in matched:
        try:
            sb.table("financial_transactions").upsert(
                txn, on_conflict="external_id,source"
            ).execute()
            assigned += 1
        except Exception as e:
            logger.warning("bulk_assign_upsert_error: %s", str(e)[:100])

    return {
        "ok": True,
        "assigned_count": assigned,
        "total_amount": sum(t["amount"] for t in matched),
        "project_id": project_id,
    }


@router.post("/search-transactions")
async def search_transactions(request: Request):
    """
    Search live Stripe transactions by criteria (without assigning).
    Same body as bulk-assign but always dry_run.
    """
    _require_super_admin(request)
    body = await request.json()
    body["dry_run"] = True

    # Reuse bulk-assign logic
    from starlette.requests import Request as StReq
    # Create a mock request with the body
    class MockReq:
        def __init__(self, orig, body):
            self.state = orig.state
            self._body = body
        async def json(self):
            return self._body

    mock = MockReq(request, body)
    return await bulk_assign_transactions(mock)


# ─── Project Profitability ──────────────────────────────────────────────────


@router.get("/profitability")
async def project_profitability(
    request: Request,
    days: int = Query(30),
    project_id: Optional[str] = None,
):
    """Per-project profitability from stored transactions."""
    _require_super_admin(request)

    # Use the RPC function if stored transactions exist
    if not project_id:
        try:
            r = sb.rpc("fn_project_profitability", {"p_days": days}).execute()
            if r.data:  # Only return if we have stored data
                return {"ok": True, "data": r.data}
        except Exception:
            pass
    # Fallback: calculate from live Stripe API data

    # Fallback: calculate from live revenue data
    # Get revenue by source for the period
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_ts = int(since.timestamp())

    # Get projects
    projects_q = sb.table("projects").select("*")
    if project_id:
        projects_q = projects_q.eq("id", project_id)
    projects = (projects_q.execute()).data or []

    # Get revenue per source from Stripe
    revenue_by_source = {}
    async with httpx.AsyncClient(timeout=45.0) as client:
        import asyncio

        async def fetch_stripe_total(acct_id, info):
            key = _get_stripe_key(acct_id)
            if not key:
                return acct_id, 0, 0, 0
            usd, mxn, count = 0, 0, 0
            try:
                has_more, starting_after, pages = True, None, 0
                while has_more and pages < 20:
                    params = {"created[gte]": since_ts, "limit": 100}
                    if starting_after:
                        params["starting_after"] = starting_after
                    r = await client.get("https://api.stripe.com/v1/payment_intents",
                                         params=params, auth=(key, ""))
                    if r.status_code != 200:
                        break
                    data = r.json()
                    for pi in data.get("data", []):
                        if pi.get("status") != "succeeded":
                            continue
                        amt = pi["amount"] / 100
                        if pi["currency"].upper() == "USD":
                            usd += amt
                        else:
                            mxn += amt
                        count += 1
                    has_more = data.get("has_more", False)
                    items = data.get("data", [])
                    if items:
                        starting_after = items[-1]["id"]
                    pages += 1
            except Exception:
                pass
            return acct_id, usd, mxn, count

        tasks = [fetch_stripe_total(aid, info) for aid, info in STRIPE_ACCOUNTS.items()]
        results = await asyncio.gather(*tasks)
        for acct_id, usd, mxn, count in results:
            revenue_by_source[acct_id] = {"usd": usd, "mxn": mxn, "count": count}

    # Map projects to their revenue
    profitability = []
    for proj in projects:
        stripe_acct = proj.get("stripe_account", "")
        rev = revenue_by_source.get(stripe_acct, {"usd": 0, "mxn": 0, "count": 0})
        profitability.append({
            "project_id": proj["id"],
            "project_name": proj["name"],
            "stripe_account": stripe_acct,
            "mercury_account": proj.get("mercury_account", ""),
            "revenue_usd": round(rev["usd"], 2),
            "revenue_mxn": round(rev["mxn"], 2),
            "transaction_count": rev["count"],
            "status": proj.get("status", "active"),
        })

    profitability.sort(key=lambda x: x["revenue_usd"], reverse=True)
    return {"ok": True, "data": profitability}


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

                # Get recent payments to calculate revenue (last 30 days)
                total_rev = 0
                total_count = 0
                thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
                for page in range(1, 15):
                    pr = await client.get(
                        f"https://api.whop.com/api/v5/company/payments?per=100&page={page}&status=paid",
                        headers=headers_whop,
                    )
                    if pr.status_code != 200:
                        break
                    payments = pr.json().get("data", [])
                    for p in payments:
                        created = p.get("created_at") or p.get("paid_at") or 0
                        if isinstance(created, (int, float)) and created > 0:
                            dt = datetime.fromtimestamp(int(created), tz=timezone.utc)
                            if dt < thirty_days_ago:
                                continue  # skip but don't break — order not guaranteed
                        else:
                            continue
                        subtotal = p.get("subtotal", 0) or 0
                        if subtotal > 0:
                            total_rev += subtotal  # already in dollars
                            total_count += 1
                    if not payments or not pr.json().get("pagination", {}).get("next_page"):
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
    whop_balance = (result.get("whop") or {}).get("balance") or {}
    if whop_balance.get("total"):
        total_usd += whop_balance["total"]
    result["totals"] = {"usd": total_usd, "mxn": total_mxn}

    return {"ok": True, "data": result}


# ─── Debug Whop ──────────────────────────────────────────────────────────


@router.get("/debug-whop")
async def debug_whop(request: Request):
    """Temporary debug endpoint for Whop payments."""
    _require_super_admin(request)
    whop_key = getattr(settings, "whop_api_key", "")
    result = {"key_exists": bool(whop_key), "key_len": len(whop_key)}
    if not whop_key:
        return {"ok": False, "data": result}

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers_whop = {"Authorization": f"Bearer {whop_key}"}
        pr = await client.get(
            "https://api.whop.com/api/v5/company/payments?per=3&page=1&status=paid",
            headers=headers_whop,
        )
        result["status"] = pr.status_code
        if pr.status_code == 200:
            data = pr.json()
            result["pagination"] = data.get("pagination", {})
            result["count"] = len(data.get("data", []))
            result["sample_payments"] = []
            for p in data.get("data", [])[:3]:
                result["sample_payments"].append({
                    "subtotal": p.get("subtotal"),
                    "final_amount": p.get("final_amount"),
                    "amount": p.get("amount"),
                    "created_at": p.get("created_at"),
                    "product_name": p.get("product_name"),
                    "status": p.get("status"),
                    "currency": p.get("currency"),
                    "keys": list(p.keys())[:20],
                })
        else:
            result["body"] = pr.text[:500]

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

    async def _fetch_stripe_account(client, acct_id, info, since_ts):
        """Fetch all payment_intents for one Stripe account with pagination."""
        key = _get_stripe_key(acct_id)
        if not key:
            return []
        results = []
        try:
            has_more = True
            starting_after = None
            page_count = 0
            while has_more and page_count < 20:
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
                    results.append({
                        "source": f"stripe_{acct_id}",
                        "source_name": info["name"],
                        "amount": pi["amount"] / 100,
                        "currency": pi["currency"].upper(),
                        "date": datetime.fromtimestamp(pi["created"], tz=timezone.utc).isoformat(),
                        "campaign_id": meta.get("campaign_id", ""),
                        "lead_id": meta.get("lead_id", ""),
                        "description": pi.get("description", ""),
                    })
                has_more = data.get("has_more", False)
                if items:
                    starting_after = items[-1]["id"]
                page_count += 1
        except Exception as e:
            logger.warning("stripe_revenue_error acct=%s err=%s", acct_id, str(e)[:80])
        return results

    async def _fetch_whop_payments(client, since):
        """Fetch Whop payments within date range."""
        whop_key = getattr(settings, "whop_api_key", "")
        if not whop_key:
            return []
        results = []
        try:
            headers_whop = {"Authorization": f"Bearer {whop_key}"}
            for page in range(1, 15):
                pr = await client.get(
                    f"https://api.whop.com/api/v5/company/payments?per=100&page={page}&status=paid",
                    headers=headers_whop,
                )
                if pr.status_code != 200:
                    break
                payments = pr.json().get("data", [])
                for p in payments:
                    created = p.get("created_at") or p.get("paid_at") or 0
                    if not created:
                        continue
                    try:
                        dt = datetime.fromtimestamp(int(created), tz=timezone.utc) if isinstance(created, (int, float)) else datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if dt < since:
                        continue
                    subtotal = p.get("subtotal", 0) or 0
                    if subtotal > 0:
                        results.append({
                            "source": "stripe_lba",
                            "source_name": "Legacy Business Academy",
                            "amount": subtotal,
                            "currency": "USD",
                            "date": dt.isoformat(),
                            "campaign_id": "",
                            "lead_id": "",
                            "description": f"Whop: {p.get('product_name') or p.get('plan_id') or 'Payment'}",
                        })
                if not payments or not pr.json().get("pagination", {}).get("next_page"):
                    break
        except Exception as e:
            logger.warning("whop_revenue_error err=%s", str(e)[:100])
        return results

    # Fetch ALL sources in PARALLEL for speed
    async with httpx.AsyncClient(timeout=45.0) as client:
        import asyncio
        tasks = []
        for acct_id, info in STRIPE_ACCOUNTS.items():
            tasks.append(_fetch_stripe_account(client, acct_id, info, since_ts))
        tasks.append(_fetch_whop_payments(client, since))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                revenue.extend(r)
            elif isinstance(r, Exception):
                logger.warning("revenue_task_error err=%s", str(r)[:100])

        # Whop is now fetched in parallel above

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


# ─── Smart Auto-Match Engine ──────────────────────────────────────────────


# Project keyword map: project display name → list of lowercase keywords to match
_PROJECT_KEYWORDS = {
    "Beyond Wealth CDMX": [
        "beyond wealth cdmx", "bw cdmx", "cdmx 2025", "cdmx 2026",
        "pase vip beyond wealth cdmx", "pase diamond beyond wealth cdmx",
        "pase general beyond wealth cdmx", "acceso diamond cdmx",
        "acceso vip beyond wealth cdmx", "libro beyond wealth cdmx",
        "pase beyond wealth cdmx", "beyond wealth || cdmx",
        "despertar millonario",
    ],
    "Beyond Wealth Miami": [
        "beyond wealth miami", "bw miami", "expert certification usa",
        "conquer wealth usa", "conquer wealth", "pase vip conquer",
        "business leader vip", "expert certification",
    ],
    "Beyond Wealth CR": [
        "beyond wealth costa rica", "bw costa rica", "bw cr",
        "acceso vip a beyond wealth costa rica", "acceso diamond a beyond wealth costa rica",
    ],
    "Beyond Wealth Lima": [
        "beyond wealth lima", "bw lima", "pase vip beyond wealth lima",
        "acceso vip beyond wealth lima", "acceso diamond beyond wealth lima",
    ],
    "Contexto Millonario LATAM": [
        "contexto millonario", "contexto", "cm latam",
        "boleto contexto", "boleto cm",
    ],
    "Contexto Millonario USA": ["contexto usa", "cm usa"],
    "Cashflow Master LATAM": [
        "cashflow", "cfm", "cashflow master",
        "1% club", "1 % club", "inner circle",
    ],
    "Cashflow Master USA": ["cashflow usa"],
    "VSL 24/7": ["vsl", "24/7", "viral", "5000 upsell viral"],
    "Mentorías Especiales": [
        "mentoría", "mentoria", "mentorías", "panda",
        "aportación legacy", "apartado legacy", "legacy mentorship",
        "owner", "legacy business", "one life legacy",
    ],
    "Viajes": ["viaje", "dubai", "trip", "apartado viaje"],
    "Podcast": ["podcast"],
    "Content Scale": ["content scale", "contenido viral"],
    "Sprint": ["sprint", "arranque", "mi primer negocio digital"],
    "Consultoría": [
        "consultoría", "consultoria", "consulting",
        "asesoria", "asesoría",
    ],
    "Escuela de Amor Consciente": ["amor consciente", "coach amor"],
    "2clicks.com": [
        "2clicks", "2 clicks", "auto-recharge", "manual recharge",
        "sub-account", "viva2clicks", "2clicks4friends",
        "todoen2clicks", "mireto100m",
    ],
}


def _match_text_to_project(
    text: str,
    projects: list[dict],
) -> tuple[dict | None, int, str]:
    """Match a product name / description to a project using keyword map.

    Returns (project, confidence, reason).
    Longest keyword match wins (more specific = better).
    """
    text_lower = text.lower().strip()
    if not text_lower:
        return None, 0, "No text"

    best_proj = None
    best_conf = 0
    best_reason = ""
    best_keyword_len = 0

    # 1) Direct project name substring match (highest confidence)
    for proj in projects:
        proj_name = (proj.get("name") or "").lower()
        if not proj_name:
            continue
        if proj_name in text_lower or text_lower in proj_name:
            return proj, 95, f"Product: '{text}'"

    # 2) Keyword map match — longest keyword wins
    for proj_label, keywords in _PROJECT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower and len(kw) > best_keyword_len:
                # Find the matching project in DB by name
                matched_proj = None
                for proj in projects:
                    pn = (proj.get("name") or "").lower()
                    if proj_label.lower() in pn or pn in proj_label.lower():
                        matched_proj = proj
                        break
                # Fallback: partial match on keywords
                if not matched_proj:
                    for proj in projects:
                        pn = (proj.get("name") or "").lower()
                        # Check if any significant word from proj_label is in project name
                        label_words = [w for w in proj_label.lower().split() if len(w) > 2]
                        if all(w in pn for w in label_words):
                            matched_proj = proj
                            break
                if matched_proj:
                    best_proj = matched_proj
                    best_keyword_len = len(kw)
                    # More specific keywords get higher confidence
                    if len(kw) > 10:
                        best_conf = 95
                    else:
                        best_conf = 85
                    best_reason = f"Product: '{text}' (keyword: '{kw}')"

    if best_proj:
        return best_proj, best_conf, best_reason

    return None, 0, "No match"


async def _fetch_stripe_invoices(
    client: httpx.AsyncClient, days: int
) -> list[dict]:
    """Fetch paid invoices with expanded line items from all Stripe accounts."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    all_items: list[dict] = []

    for label in STRIPE_ACCOUNTS:
        key = _get_stripe_key(label)
        if not key:
            continue
        source = f"stripe_{label}"
        cursor = None
        for _ in range(50):
            params: dict = {
                "created[gte]": cutoff,
                "limit": 100,
                "status": "paid",
            }
            if cursor:
                params["starting_after"] = cursor
            r = await client.get(
                "https://api.stripe.com/v1/invoices",
                params=params, auth=(key, ""),
            )
            if r.status_code != 200:
                logger.warning("smart_match invoice_error account=%s status=%s", label, r.status_code)
                break
            data = r.json()
            page_items = data.get("data", [])
            for inv in page_items:
                inv_id = inv["id"]
                inv_total = (inv.get("amount_paid") or inv.get("total") or 0) / 100
                inv_currency = (inv.get("currency") or "usd").upper()
                dt = datetime.fromtimestamp(inv["created"], tz=timezone.utc)
                email = inv.get("customer_email") or ""

                # Extract product names from line items
                lines = (inv.get("lines") or {}).get("data", [])
                product_names = []
                for line in lines:
                    # Try expanded product object first
                    price_obj = line.get("price") or {}
                    product_obj = price_obj.get("product")
                    if isinstance(product_obj, dict):
                        pname = product_obj.get("name") or ""
                    elif isinstance(product_obj, str):
                        pname = ""  # product not expanded, just an ID
                    else:
                        pname = ""
                    # Fallback to line description
                    if not pname:
                        pname = line.get("description") or ""
                    if pname and pname not in product_names:
                        product_names.append(pname)

                # Use the combined product name or invoice description
                description = " | ".join(product_names) if product_names else (inv.get("description") or f"Invoice {inv_id}")

                all_items.append({
                    "id": inv_id,
                    "amount": inv_total,
                    "currency": inv_currency,
                    "description": description,
                    "product_names": product_names,
                    "source": source,
                    "account": label,
                    "created": dt.isoformat(),
                    "receipt_email": email,
                    "type": "invoice",
                })
                cursor = inv_id
            if not data.get("has_more"):
                break
    return all_items


async def _fetch_noninvoice_payment_intents(
    client: httpx.AsyncClient, days: int, invoice_pi_ids: set[str]
) -> list[dict]:
    """Fetch one-time payment_intents that don't have invoices."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    all_pis: list[dict] = []

    for label in STRIPE_ACCOUNTS:
        key = _get_stripe_key(label)
        if not key:
            continue
        source = f"stripe_{label}"
        cursor = None
        for _ in range(50):
            params: dict = {
                "limit": 100,
                "created[gte]": cutoff,
                "expand[]": "data.latest_charge",
            }
            if cursor:
                params["starting_after"] = cursor
            r = await client.get(
                "https://api.stripe.com/v1/payment_intents",
                params=params, auth=(key, ""),
            )
            if r.status_code != 200:
                logger.warning("smart_match pi_error account=%s status=%s", label, r.status_code)
                break
            data = r.json()
            page_items = data.get("data", [])
            for pi in page_items:
                if pi.get("status") != "succeeded":
                    continue
                # Skip if this PI was already captured via an invoice
                if pi.get("invoice"):
                    continue
                amt = pi["amount"] / 100
                # Get description from charge if available
                charge = pi.get("latest_charge")
                if isinstance(charge, dict):
                    desc = charge.get("description") or pi.get("description") or ""
                    stmt = charge.get("statement_descriptor") or ""
                    if not desc and stmt:
                        desc = stmt
                else:
                    desc = pi.get("description") or ""
                dt = datetime.fromtimestamp(pi["created"], tz=timezone.utc)
                all_pis.append({
                    "id": pi["id"],
                    "amount": amt,
                    "currency": (pi.get("currency") or "usd").upper(),
                    "description": desc,
                    "product_names": [desc] if desc else [],
                    "source": source,
                    "account": label,
                    "created": dt.isoformat(),
                    "receipt_email": pi.get("receipt_email") or "",
                    "type": "payment_intent",
                })
                cursor = pi["id"]
            if not data.get("has_more"):
                break
    return all_pis


def _clean_suggested_name(raw: str) -> str:
    """Clean up a product name to suggest as a project name."""
    import re
    name = raw.strip()
    # Remove common prefixes/noise words
    noise = ["APARTADO", "PASE", "PAGO", "CUOTA", "ABONO", "INSCRIPCION", "INSCRIPCIÓN", "REGISTRO"]
    for word in noise:
        name = re.sub(rf'\b{word}\b', '', name, flags=re.IGNORECASE)
    # Remove leading/trailing numbers, dashes, spaces
    name = re.sub(r'^[\d\s\-–—]+', '', name)
    name = re.sub(r'[\d\s\-–—]+$', '', name)
    name = name.strip(" -–—")
    # Title case
    if name == name.upper() and len(name) > 3:
        name = name.title()
    return name or raw.strip().title()


def _similarity_score(a: str, b: str) -> int:
    """Simple keyword-overlap similarity between two strings (0-100)."""
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    if not a_words or not b_words:
        return 0
    # Check substring containment first
    a_lower, b_lower = a.lower(), b.lower()
    if a_lower in b_lower or b_lower in a_lower:
        return 85
    # Word overlap
    overlap = a_words & b_words
    if not overlap:
        # Check partial word matching (e.g. "dubai" in "viaje dubai")
        for wa in a_words:
            for wb in b_words:
                if len(wa) > 3 and (wa in wb or wb in wa):
                    return 60
        return 0
    return min(100, int(len(overlap) / max(len(a_words), len(b_words)) * 100))


def _build_new_project_suggestions(
    unmatched: list[dict],
    projects: list[dict],
) -> list[dict]:
    """Group unmatched transactions by product name and suggest new projects.

    Only groups with 2+ transactions are suggested.
    """
    # Group by normalized product name
    groups: dict[str, dict] = {}
    for txn in unmatched:
        # Use first product name if available, else description
        product_names = txn.get("product_names") or []
        label = product_names[0] if product_names else (txn.get("description") or "")
        if not label or not label.strip():
            continue
        norm = label.lower().strip()
        if norm not in groups:
            groups[norm] = {
                "raw_name": label.strip(),
                "product_names": set(),
                "transaction_ids": [],
                "total_usd": 0.0,
                "total_mxn": 0.0,
                "sources": set(),
                "sample_descriptions": [],
            }
        g = groups[norm]
        g["product_names"].add(label.strip())
        g["transaction_ids"].append(txn["id"])
        g["sources"].add(txn["source"])
        if txn["currency"] == "MXN":
            g["total_mxn"] += txn["amount"]
        else:
            g["total_usd"] += txn["amount"]
        desc = txn.get("description") or ""
        if desc and len(g["sample_descriptions"]) < 5 and desc not in g["sample_descriptions"]:
            g["sample_descriptions"].append(desc)

    # Build suggestions for groups with 2+ transactions
    suggestions = []
    for _norm, g in groups.items():
        if len(g["transaction_ids"]) < 2:
            continue

        suggested_name = _clean_suggested_name(g["raw_name"])

        # Find closest existing project
        closest = None
        best_sim = 0
        for proj in projects:
            pname = proj.get("name") or ""
            sim = _similarity_score(suggested_name, pname)
            if sim > best_sim:
                best_sim = sim
                closest = proj

        closest_info = None
        if closest and best_sim > 0:
            closest_info = {
                "id": closest["id"],
                "name": closest.get("name", ""),
                "similarity": best_sim,
            }

        suggestions.append({
            "suggested_name": suggested_name,
            "product_names": sorted(g["product_names"]),
            "transaction_count": len(g["transaction_ids"]),
            "total_usd": round(g["total_usd"], 2),
            "total_mxn": round(g["total_mxn"], 2),
            "sources": sorted(g["sources"]),
            "sample_descriptions": g["sample_descriptions"],
            "transaction_ids": g["transaction_ids"],
            "closest_existing_project": closest_info,
        })

    # Sort by total value descending
    suggestions.sort(key=lambda x: x["total_usd"] + x["total_mxn"], reverse=True)
    return suggestions


def _run_smart_match(
    projects: list[dict],
    transactions: list[dict],
    already_assigned: set[str],
) -> dict:
    """Core matching logic using invoice line-item product names.

    Groups by project_id so each project gets ONE suggestion card.
    Returns suggestions + unmatched stats.
    """
    # bucket key = project_id
    buckets: dict[str, dict] = {}
    unmatched: list[dict] = []

    for txn in transactions:
        if txn["id"] in already_assigned:
            continue

        product_names = txn.get("product_names") or []
        desc = txn["description"]
        amt = txn["amount"]

        best_proj = None
        best_conf = 0
        best_reason = ""
        best_product = desc  # label for the bucket

        # Try each product name from invoice line items
        for pname in product_names:
            proj, conf, reason = _match_text_to_project(pname, projects)
            if conf > best_conf:
                best_proj, best_conf, best_reason = proj, conf, reason
                best_product = pname

        # If no match from product names, try the full description
        if best_conf == 0 and desc:
            proj, conf, reason = _match_text_to_project(desc, projects)
            if conf > best_conf:
                best_proj, best_conf, best_reason = proj, conf, reason
                best_product = desc

        # Amount-only fallback for generic descriptions (low confidence)
        if best_conf == 0 and amt:
            best_product = desc or f"${amt} payment"
            best_conf = 0  # leave unmatched — amount guessing was too inaccurate

        if best_proj and best_conf > 0:
            pid = best_proj["id"]
            bucket_key = pid
            if bucket_key not in buckets:
                buckets[bucket_key] = {
                    "project_id": pid,
                    "project_name": best_proj.get("name", ""),
                    "confidence": best_conf,
                    "match_reason": best_reason,
                    "total_usd": 0.0,
                    "total_mxn": 0.0,
                    "transaction_ids": [],
                    "sample_descriptions": [],
                    "sources": set(),
                }
            b = buckets[bucket_key]
            if best_conf > b["confidence"]:
                b["confidence"] = best_conf
                b["match_reason"] = best_reason
            if txn["currency"] == "MXN":
                b["total_mxn"] += amt
            else:
                b["total_usd"] += amt
            b["transaction_ids"].append(txn["id"])
            b["sources"].add(txn["source"])
            if len(b["sample_descriptions"]) < 5 and desc:
                if desc not in b["sample_descriptions"]:
                    b["sample_descriptions"].append(desc)
        else:
            unmatched.append(txn)

    # Serialize
    suggestions = []
    for b in sorted(buckets.values(), key=lambda x: (-x["confidence"], -x["total_usd"])):
        suggestions.append({
            "project_id": b["project_id"],
            "project_name": b["project_name"],
            "confidence": b["confidence"],
            "match_reason": b["match_reason"],
            "transaction_count": len(b["transaction_ids"]),
            "total_usd": round(b["total_usd"], 2),
            "total_mxn": round(b["total_mxn"], 2),
            "sample_descriptions": b["sample_descriptions"],
            "transaction_ids": b["transaction_ids"],
            "sources": sorted(b["sources"]),
        })

    unmatched_usd = sum(t["amount"] for t in unmatched if t["currency"] != "MXN")

    # ── New project suggestions from unmatched transactions ──
    new_project_suggestions = _build_new_project_suggestions(unmatched, projects)

    # Sample unmatched descriptions for debugging
    unmatched_samples = []
    seen_descs: set = set()
    for t in sorted(unmatched, key=lambda x: -x["amount"])[:50]:
        desc = t["description"][:100]
        if desc not in seen_descs:
            seen_descs.add(desc)
            unmatched_samples.append({
                "description": desc,
                "amount": t["amount"],
                "currency": t["currency"],
                "source": t["source"],
            })

    return {
        "suggestions": suggestions,
        "new_project_suggestions": new_project_suggestions,
        "unmatched_count": len(unmatched),
        "unmatched_total_usd": round(unmatched_usd, 2),
        "unmatched_samples": unmatched_samples[:30],
        "total_unassigned": len(transactions) - len(already_assigned & {t["id"] for t in transactions}),
    }


async def _get_already_assigned_ids() -> set[str]:
    """Get external_ids that already have a project_id in financial_transactions."""
    try:
        rows = (
            sb.table("financial_transactions")
            .select("external_id")
            .not_.is_("project_id", "null")
            .execute()
        )
        return {r["external_id"] for r in (rows.data or [])}
    except Exception as e:
        logger.warning("smart_match_assigned_check_error: %s", str(e)[:100])
        return set()


async def _fetch_smart_match_transactions(client: httpx.AsyncClient, days: int) -> tuple[list[dict], list[dict]]:
    """Fetch invoices + non-invoice PIs for smart matching. Returns (all_transactions, stripe_products)."""
    # Fetch invoices with product details
    invoices = await _fetch_stripe_invoices(client, days)

    # Fetch one-time payment_intents (those without invoices)
    invoice_pi_ids: set[str] = set()  # invoices don't have PI ids directly, we skip by checking pi.invoice
    one_time_pis = await _fetch_noninvoice_payment_intents(client, days, invoice_pi_ids)

    all_transactions = invoices + one_time_pis

    # Build a simple products list from what we found
    seen_products: dict[str, dict] = {}
    for txn in all_transactions:
        for pname in txn.get("product_names", []):
            if pname and pname not in seen_products:
                seen_products[pname] = {
                    "id": pname,
                    "name": pname,
                    "account": txn["account"],
                }
    stripe_products = list(seen_products.values())

    return all_transactions, stripe_products


@router.get("/smart-match")
async def smart_match(request: Request, days: int = Query(30)):
    """Smart transaction matching using invoice line-item product names."""
    _require_super_admin(request)

    # 1. Fetch projects from Supabase
    proj_resp = sb.table("projects").select("*").execute()
    projects = proj_resp.data or []

    # 2. Get already-assigned transaction IDs
    already_assigned = await _get_already_assigned_ids()

    async with httpx.AsyncClient(timeout=60) as client:
        # 3. Fetch invoices + one-time PIs with product details
        all_transactions, stripe_products = await _fetch_smart_match_transactions(client, days)

    logger.info(
        "smart_match invoices+pis=%d projects=%d already_assigned=%d products=%d",
        len(all_transactions), len(projects), len(already_assigned), len(stripe_products),
    )

    # 4. Run matching
    result = _run_smart_match(projects, all_transactions, already_assigned)
    result["stripe_products"] = stripe_products

    return {"ok": True, "data": result}


@router.post("/smart-match/approve")
async def smart_match_approve(request: Request):
    """Approve a suggestion batch: assign transaction_ids to a project."""
    _require_super_admin(request)
    body = await request.json()

    project_id = body.get("project_id")
    transaction_ids = body.get("transaction_ids", [])
    if not project_id:
        raise HTTPException(400, "project_id required")
    if not transaction_ids:
        raise HTTPException(400, "transaction_ids required")

    # Verify project exists
    proj = sb.table("projects").select("id,name").eq("id", project_id).limit(1).execute()
    if not proj.data:
        raise HTTPException(404, "Project not found")

    assigned = 0
    skipped = 0
    for ext_id in transaction_ids:
        try:
            # Check if already assigned
            existing = (
                sb.table("financial_transactions")
                .select("id,project_id")
                .eq("external_id", ext_id)
                .limit(1)
                .execute()
            )
            if existing.data and existing.data[0].get("project_id"):
                skipped += 1
                continue

            if existing.data:
                # Update existing row
                sb.table("financial_transactions").update({
                    "project_id": project_id,
                    "auto_assigned": True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("external_id", ext_id).execute()
            else:
                # Create new row
                sb.table("financial_transactions").insert({
                    "external_id": ext_id,
                    "source": "stripe",
                    "type": "sale",
                    "project_id": project_id,
                    "auto_assigned": True,
                    "amount": 0,
                    "currency": "USD",
                    "description": f"Smart-matched to {proj.data[0]['name']}",
                }).execute()
            assigned += 1
        except Exception as e:
            logger.warning("smart_match_approve_error ext_id=%s: %s", ext_id, str(e)[:100])

    return {
        "ok": True,
        "assigned": assigned,
        "skipped": skipped,
        "project_id": project_id,
    }


@router.post("/smart-match/approve-all")
async def smart_match_approve_all(request: Request, days: int = Query(30)):
    """Approve ALL high-confidence suggestions at once."""
    _require_super_admin(request)
    body = await request.json()
    min_confidence = body.get("min_confidence", 85)

    # Run smart-match internally
    proj_resp = sb.table("projects").select("*").execute()
    projects = proj_resp.data or []
    already_assigned = await _get_already_assigned_ids()

    async with httpx.AsyncClient(timeout=60) as client:
        all_transactions, _products = await _fetch_smart_match_transactions(client, days)

    result = _run_smart_match(projects, all_transactions, already_assigned)

    total_assigned = 0
    total_skipped = 0
    approved_projects = []

    for suggestion in result["suggestions"]:
        if suggestion["confidence"] < min_confidence:
            continue

        project_id = suggestion["project_id"]
        assigned = 0
        skipped = 0

        for ext_id in suggestion["transaction_ids"]:
            try:
                existing = (
                    sb.table("financial_transactions")
                    .select("id,project_id")
                    .eq("external_id", ext_id)
                    .limit(1)
                    .execute()
                )
                if existing.data and existing.data[0].get("project_id"):
                    skipped += 1
                    continue

                if existing.data:
                    sb.table("financial_transactions").update({
                        "project_id": project_id,
                        "auto_assigned": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("external_id", ext_id).execute()
                else:
                    sb.table("financial_transactions").insert({
                        "external_id": ext_id,
                        "source": "stripe",
                        "type": "sale",
                        "project_id": project_id,
                        "auto_assigned": True,
                        "amount": 0,
                        "currency": "USD",
                        "description": f"Smart-matched to {suggestion['project_name']}",
                    }).execute()
                assigned += 1
            except Exception as e:
                logger.warning("approve_all_error ext_id=%s: %s", ext_id, str(e)[:100])

        total_assigned += assigned
        total_skipped += skipped
        approved_projects.append({
            "project_id": project_id,
            "project_name": suggestion["project_name"],
            "confidence": suggestion["confidence"],
            "assigned": assigned,
            "skipped": skipped,
        })

    return {
        "ok": True,
        "min_confidence": min_confidence,
        "total_assigned": total_assigned,
        "total_skipped": total_skipped,
        "approved_projects": approved_projects,
    }
