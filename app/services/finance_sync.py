"""Finance sync engine — pulls transactions from Stripe, Mercury, Whop
and persists them in financial_transactions table with project linking."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("finance_sync")

# ─── Account maps (shared with finance.py) ────────────────────────────────

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

STRIPE_KEY_MAP = {
    "uvul": lambda: getattr(settings, "stripe_key_uvul", ""),
    "lba": lambda: getattr(settings, "stripe_key_lba", ""),
    "oll": lambda: getattr(settings, "stripe_key_oll", ""),
    "2clicks": lambda: getattr(settings, "stripe_key_2clicks", ""),
}

MERCURY_KEY_MAP = {
    "oll": lambda: getattr(settings, "mercury_key_oll", ""),
    "2clicks": lambda: getattr(settings, "mercury_key_2clicks", ""),
    "lba": lambda: getattr(settings, "mercury_key_lba", ""),
}


# ─── Cursor helpers ────────────────────────────────────────────────────────

def _get_cursor(source_id: str) -> Optional[datetime]:
    """Get last sync timestamp for a source."""
    try:
        r = sb.table("sync_cursors").select("last_synced_at").eq("id", source_id).limit(1).execute()
        if r.data and r.data[0].get("last_synced_at"):
            return datetime.fromisoformat(r.data[0]["last_synced_at"].replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _set_cursor(source_id: str, synced_at: datetime):
    """Update sync cursor for a source."""
    try:
        sb.table("sync_cursors").upsert({
            "id": source_id,
            "last_synced_at": synced_at.isoformat(),
            "updated_at": "now()",
        }, on_conflict="id").execute()
    except Exception as e:
        logger.warning("cursor_update_error source=%s err=%s", source_id, str(e)[:80])


# ─── Assignment rules engine ──────────────────────────────────────────────

def _load_rules() -> list[dict]:
    """Load all enabled assignment rules, ordered by priority desc."""
    try:
        r = sb.table("transaction_assignment_rules").select("*").eq("enabled", True).order("priority", desc=True).execute()
        return r.data or []
    except Exception:
        return []


def _match_rule(txn: dict, rule: dict) -> bool:
    """Check if a transaction matches an assignment rule."""
    field = rule["field"]
    operator = rule["operator"]
    value = rule["value"]

    # Get the field value from the transaction
    if field.startswith("metadata."):
        meta_key = field.split(".", 1)[1]
        txn_value = str(txn.get("metadata", {}).get(meta_key, ""))
    else:
        txn_value = str(txn.get(field, ""))

    if not txn_value:
        return False

    if operator == "equals":
        return txn_value == value
    elif operator == "contains":
        return value.lower() in txn_value.lower()
    elif operator == "starts_with":
        return txn_value.lower().startswith(value.lower())
    return False


def _find_project_for_txn(txn: dict, rules: list[dict]) -> Optional[str]:
    """Find the first matching project_id for a transaction."""
    for rule in rules:
        if _match_rule(txn, rule):
            return rule["project_id"]
    return None


# ─── Upsert helper ─────────────────────────────────────────────────────────

def _upsert_transactions(txns: list[dict]) -> int:
    """Upsert a batch of transactions. Returns count upserted."""
    if not txns:
        return 0
    count = 0
    # Upsert in batches of 50
    for i in range(0, len(txns), 50):
        batch = txns[i:i + 50]
        try:
            sb.table("financial_transactions").upsert(
                batch, on_conflict="external_id,source"
            ).execute()
            count += len(batch)
        except Exception as e:
            logger.warning("upsert_error batch=%d err=%s", i, str(e)[:120])
    return count


# ─── Stripe sync ───────────────────────────────────────────────────────────

async def sync_stripe(acct_id: str) -> dict:
    """Sync payment_intents from one Stripe account."""
    source = f"stripe_{acct_id}"
    info = STRIPE_ACCOUNTS.get(acct_id)
    key_fn = STRIPE_KEY_MAP.get(acct_id)
    if not info or not key_fn:
        return {"source": source, "error": "unknown account"}
    key = key_fn()
    if not key:
        return {"source": source, "error": "no API key"}

    # Get cursor — default to 1 year ago
    cursor_dt = _get_cursor(source)
    if not cursor_dt:
        cursor_dt = datetime.now(timezone.utc) - timedelta(days=365)
    since_ts = int(cursor_dt.timestamp())

    rules = _load_rules()
    txns = []
    latest_dt = cursor_dt

    async with httpx.AsyncClient(timeout=30.0) as client:
        starting_after = None
        for _ in range(50):  # max 5000 transactions
            params = {"created[gte]": since_ts, "limit": 100}
            if starting_after:
                params["starting_after"] = starting_after
            r = await client.get("https://api.stripe.com/v1/payment_intents",
                                 params=params, auth=(key, ""))
            if r.status_code != 200:
                logger.warning("stripe_sync_error acct=%s status=%s", acct_id, r.status_code)
                break
            data = r.json()
            items = data.get("data", [])
            for pi in items:
                if pi.get("status") != "succeeded":
                    continue
                meta = pi.get("metadata") or {}
                dt = datetime.fromtimestamp(pi["created"], tz=timezone.utc)
                if dt > latest_dt:
                    latest_dt = dt
                txn = {
                    "external_id": pi["id"],
                    "source": source,
                    "type": "sale",
                    "amount": round(pi["amount"] / 100, 2),
                    "currency": pi["currency"].upper(),
                    "txn_date": dt.isoformat(),
                    "description": pi.get("description") or "Stripe payment",
                    "counterparty": pi.get("receipt_email") or pi.get("customer") or "",
                    "metadata": {
                        "campaign_id": meta.get("campaign_id", ""),
                        "lead_id": meta.get("lead_id", ""),
                        "customer": pi.get("customer", ""),
                        "payment_method": pi.get("payment_method_types", []),
                    },
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                # Auto-assign project
                pid = _find_project_for_txn(txn, rules)
                if pid:
                    txn["project_id"] = pid
                    txn["auto_assigned"] = True
                txns.append(txn)

            if not data.get("has_more") or not items:
                break
            starting_after = items[-1]["id"]

        # Also sync refunds
        starting_after = None
        for _ in range(10):
            params = {"created[gte]": since_ts, "limit": 100}
            if starting_after:
                params["starting_after"] = starting_after
            r = await client.get("https://api.stripe.com/v1/refunds",
                                 params=params, auth=(key, ""))
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("data", [])
            for ref in items:
                if ref.get("status") != "succeeded":
                    continue
                dt = datetime.fromtimestamp(ref["created"], tz=timezone.utc)
                txn = {
                    "external_id": ref["id"],
                    "source": source,
                    "type": "refund",
                    "amount": round(ref["amount"] / 100, 2),
                    "currency": ref["currency"].upper(),
                    "txn_date": dt.isoformat(),
                    "description": f"Refund for {ref.get('payment_intent', '')}",
                    "counterparty": "",
                    "metadata": {"payment_intent": ref.get("payment_intent", "")},
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                pid = _find_project_for_txn(txn, rules)
                if pid:
                    txn["project_id"] = pid
                    txn["auto_assigned"] = True
                txns.append(txn)
            if not data.get("has_more") or not items:
                break
            starting_after = items[-1]["id"]

    count = _upsert_transactions(txns)
    if latest_dt > cursor_dt:
        _set_cursor(source, latest_dt)

    return {"source": source, "synced": count, "total_fetched": len(txns)}


# ─── Mercury sync ──────────────────────────────────────────────────────────

async def sync_mercury(acct_id: str) -> dict:
    """Sync transactions from one Mercury account."""
    source = f"mercury_{acct_id}"
    info = MERCURY_ACCOUNTS.get(acct_id)
    key_fn = MERCURY_KEY_MAP.get(acct_id)
    if not info or not key_fn:
        return {"source": source, "error": "unknown account"}
    key = key_fn()
    if not key:
        return {"source": source, "error": "no API key"}

    cursor_dt = _get_cursor(source)
    if not cursor_dt:
        cursor_dt = datetime.now(timezone.utc) - timedelta(days=365)
    since_str = cursor_dt.strftime("%Y-%m-%d")

    # Detect own Mercury account names for transfer detection
    own_mercury_names = set()
    for mid, minfo in MERCURY_ACCOUNTS.items():
        own_mercury_names.add(minfo["name"].lower())

    rules = _load_rules()
    txns = []
    latest_dt = cursor_dt

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {key}"}
        r = await client.get("https://api.mercury.com/api/v1/accounts", headers=headers)
        if r.status_code != 200:
            return {"source": source, "error": f"accounts fetch failed: {r.status_code}"}

        merc_accounts = r.json().get("accounts", r.json()) if isinstance(r.json(), dict) else r.json()

        for ma in merc_accounts:
            if not isinstance(ma, dict) or not ma.get("id"):
                continue
            tr = await client.get(
                f"https://api.mercury.com/api/v1/account/{ma['id']}/transactions",
                params={"start": since_str, "limit": 500},
                headers=headers,
            )
            if tr.status_code != 200:
                continue
            raw = tr.json().get("transactions", tr.json()) if isinstance(tr.json(), dict) else tr.json()
            for t in (raw if isinstance(raw, list) else []):
                amt = t.get("amount", 0)
                posted = t.get("postedDate", t.get("createdAt", ""))
                if posted:
                    try:
                        dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt > latest_dt:
                            latest_dt = dt
                    except (ValueError, AttributeError):
                        dt = datetime.now(timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)

                counterparty = t.get("counterpartyName", "")
                # Detect internal transfers
                is_transfer = counterparty.lower() in own_mercury_names

                txn = {
                    "external_id": str(t.get("id", t.get("dashboardLink", ""))),
                    "source": source,
                    "type": "transfer" if is_transfer else ("income" if amt > 0 else "expense"),
                    "amount": round(abs(amt), 2),
                    "currency": "USD",
                    "txn_date": dt.isoformat(),
                    "description": t.get("bankDescription", t.get("note", "")),
                    "counterparty": counterparty,
                    "metadata": {
                        "account_name": ma.get("name", ""),
                        "category": (t.get("details") or {}).get("category", ""),
                        "status": t.get("status", ""),
                    },
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                pid = _find_project_for_txn(txn, rules)
                if pid:
                    txn["project_id"] = pid
                    txn["auto_assigned"] = True
                txns.append(txn)

    count = _upsert_transactions(txns)
    if latest_dt > cursor_dt:
        _set_cursor(source, latest_dt)

    return {"source": source, "synced": count, "total_fetched": len(txns)}


# ─── Whop sync ─────────────────────────────────────────────────────────────

async def sync_whop() -> dict:
    """Sync payments from Whop."""
    source = "whop"
    whop_key = getattr(settings, "whop_api_key", "")
    if not whop_key:
        return {"source": source, "error": "no API key"}

    cursor_dt = _get_cursor(source)
    if not cursor_dt:
        cursor_dt = datetime.now(timezone.utc) - timedelta(days=365)

    rules = _load_rules()
    txns = []
    latest_dt = cursor_dt

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {whop_key}"}
        cursor = None
        for _ in range(50):
            params = {"per": 100, "status": "paid",
                      "created_after": cursor_dt.isoformat()}
            if cursor:
                params["cursor"] = cursor
            pr = await client.get(
                "https://api.whop.com/api/v5/company/payments",
                params=params, headers=headers,
            )
            if pr.status_code != 200:
                break
            body = pr.json()
            payments = body.get("data", [])
            for p in payments:
                amt = p.get("final_amount", p.get("subtotal", 0)) or 0
                if amt > 10000:
                    amt = amt / 100
                created = p.get("created_at", p.get("updated_at", ""))
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt > latest_dt:
                            latest_dt = dt
                    except (ValueError, AttributeError):
                        dt = datetime.now(timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)

                txn = {
                    "external_id": p.get("id", ""),
                    "source": source,
                    "type": "sale",
                    "amount": round(amt, 2),
                    "currency": (p.get("currency", "usd") or "usd").upper(),
                    "txn_date": dt.isoformat(),
                    "description": p.get("product_name", p.get("plan_name", "Whop payment")),
                    "counterparty": p.get("user_email", p.get("email", "")),
                    "metadata": {
                        "product_name": p.get("product_name", ""),
                        "plan_name": p.get("plan_name", ""),
                        "membership_id": p.get("membership_id", ""),
                    },
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                pid = _find_project_for_txn(txn, rules)
                if pid:
                    txn["project_id"] = pid
                    txn["auto_assigned"] = True
                txns.append(txn)

            pagination = body.get("pagination", {})
            cursor = pagination.get("next_cursor", pagination.get("next_page"))
            if not cursor:
                break

    count = _upsert_transactions(txns)
    if latest_dt > cursor_dt:
        _set_cursor(source, latest_dt)

    return {"source": source, "synced": count, "total_fetched": len(txns)}


# ─── Apply rules to unassigned ─────────────────────────────────────────────

def apply_rules_to_unassigned() -> dict:
    """Apply assignment rules to all transactions without a project_id."""
    rules = _load_rules()
    if not rules:
        return {"assigned": 0, "message": "no rules configured"}

    # Get unassigned transactions
    try:
        r = sb.table("financial_transactions").select("id,source,counterparty,description,metadata").is_("project_id", "null").execute()
    except Exception as e:
        return {"assigned": 0, "error": str(e)[:120]}

    unassigned = r.data or []
    assigned = 0
    for txn in unassigned:
        pid = _find_project_for_txn(txn, rules)
        if pid:
            try:
                sb.table("financial_transactions").update({
                    "project_id": pid,
                    "auto_assigned": True,
                    "updated_at": "now()",
                }).eq("id", txn["id"]).execute()
                assigned += 1
            except Exception:
                pass

    return {"assigned": assigned, "total_unassigned": len(unassigned)}


# ─── Orchestrator ──────────────────────────────────────────────────────────

async def sync_all() -> dict:
    """Sync all sources and apply assignment rules."""
    results = []
    errors = []

    # Stripe
    for acct_id in STRIPE_ACCOUNTS:
        try:
            r = await sync_stripe(acct_id)
            results.append(r)
            if "error" in r:
                errors.append(r)
        except Exception as e:
            errors.append({"source": f"stripe_{acct_id}", "error": str(e)[:120]})

    # Mercury
    for acct_id in MERCURY_ACCOUNTS:
        try:
            r = await sync_mercury(acct_id)
            results.append(r)
            if "error" in r:
                errors.append(r)
        except Exception as e:
            errors.append({"source": f"mercury_{acct_id}", "error": str(e)[:120]})

    # Whop
    try:
        r = await sync_whop()
        results.append(r)
        if "error" in r:
            errors.append(r)
    except Exception as e:
        errors.append({"source": "whop", "error": str(e)[:120]})

    # Apply rules
    rules_result = apply_rules_to_unassigned()

    total_synced = sum(r.get("synced", 0) for r in results)
    return {
        "total_synced": total_synced,
        "sources": results,
        "rules_applied": rules_result,
        "errors": errors,
    }
