"""AI Financial Advisor — analyzes all financial data and gives actionable insights."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("ai_finance_advisor")

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


async def gather_financial_snapshot(days: int = 30) -> dict[str, Any]:
    """Gather all financial data into a structured snapshot for AI analysis."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    snapshot: dict[str, Any] = {
        "period_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "revenue_by_source": {},
        "expenses_by_category": {},
        "recurring_expenses": [],
        "top_expenses": [],
        "daily_revenue": {},
        "balances": {},
        "mercury_transactions": [],
    }

    # ── 1. Stripe revenue from all accounts ──
    for key, meta in STRIPE_ACCOUNTS.items():
        sk = getattr(settings, f"stripe_secret_key_{key}", "")
        if not sk:
            continue
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                total = 0
                count = 0
                cursor = None
                for _ in range(5):
                    params = {"limit": 100, "created[gte]": int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())}
                    if cursor:
                        params["starting_after"] = cursor
                    r = await client.get(
                        "https://api.stripe.com/v1/payment_intents",
                        params=params,
                        auth=(sk, ""),
                    )
                    if r.status_code != 200:
                        break
                    data = r.json()
                    for pi in data.get("data", []):
                        if pi.get("status") == "succeeded":
                            amt = (pi.get("amount", 0) or 0) / 100
                            total += amt
                            count += 1
                    if not data.get("has_more"):
                        break
                    items = data.get("data", [])
                    if items:
                        cursor = items[-1]["id"]

                snapshot["revenue_by_source"][meta["name"]] = {
                    "amount": round(total, 2),
                    "currency": meta["currency"],
                    "transactions": count,
                }
        except Exception as e:
            logger.warning("stripe_snapshot_error key=%s err=%s", key, str(e)[:80])

    # ── 2. Mercury transactions (expenses + income) ──
    for key, meta in MERCURY_ACCOUNTS.items():
        token = getattr(settings, f"mercury_api_token_{key}", "")
        acc_id = getattr(settings, f"mercury_account_id_{key}", "")
        if not token or not acc_id:
            continue
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    f"https://api.mercury.com/api/v1/account/{acc_id}/transactions",
                    params={"limit": 500, "start": since[:10], "end": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    continue
                txns = r.json().get("transactions", [])

                expenses = {}
                income_total = 0
                for t in txns:
                    amt = abs(t.get("amount", 0))
                    counterparty = t.get("counterpartyName", t.get("counterpartyNickname", "Unknown"))
                    is_expense = t.get("amount", 0) < 0

                    snapshot["mercury_transactions"].append({
                        "company": meta["name"],
                        "counterparty": counterparty,
                        "amount": round(amt, 2),
                        "type": "expense" if is_expense else "income",
                        "date": t.get("postedDate", t.get("createdAt", "")),
                        "note": t.get("note", ""),
                    })

                    if is_expense:
                        if counterparty not in expenses:
                            expenses[counterparty] = {"total": 0, "count": 0, "amounts": []}
                        expenses[counterparty]["total"] += amt
                        expenses[counterparty]["count"] += 1
                        expenses[counterparty]["amounts"].append(amt)
                    else:
                        income_total += amt

                # Identify recurring expenses (same counterparty, 2+ transactions)
                for cp, info in expenses.items():
                    if info["count"] >= 2:
                        snapshot["recurring_expenses"].append({
                            "company": meta["name"],
                            "counterparty": cp,
                            "total": round(info["total"], 2),
                            "count": info["count"],
                            "avg_amount": round(info["total"] / info["count"], 2),
                        })

                snapshot["expenses_by_category"][meta["name"]] = {
                    "total_expenses": round(sum(v["total"] for v in expenses.values()), 2),
                    "total_income": round(income_total, 2),
                    "unique_vendors": len(expenses),
                    "top_vendors": sorted(
                        [{"name": k, "total": round(v["total"], 2), "count": v["count"]}
                         for k, v in expenses.items()],
                        key=lambda x: x["total"], reverse=True
                    )[:15],
                }
        except Exception as e:
            logger.warning("mercury_snapshot_error key=%s err=%s", key, str(e)[:80])

    # ── 3. Sort recurring expenses by total (biggest first) ──
    snapshot["recurring_expenses"].sort(key=lambda x: x["total"], reverse=True)

    # ── 4. Top single expenses across all companies ──
    all_expenses = sorted(
        [t for t in snapshot["mercury_transactions"] if t["type"] == "expense"],
        key=lambda x: x["amount"], reverse=True
    )
    snapshot["top_expenses"] = all_expenses[:20]

    # Remove raw transactions to keep payload manageable for AI
    del snapshot["mercury_transactions"]

    return snapshot


async def generate_ai_insights(snapshot: dict, language: str = "es") -> dict:
    """Send financial snapshot to Claude and get actionable insights."""
    api_key = settings.anthropic_api_key
    if not api_key:
        return {"error": "Anthropic API key not configured"}

    lang_instruction = "Respond entirely in Spanish." if language == "es" else "Respond in English."

    system_prompt = f"""You are an elite CFO and financial advisor for a multi-company enterprise.
You analyze financial data with surgical precision and give ACTIONABLE, SPECIFIC recommendations.

{lang_instruction}

Your analysis style:
- Be direct and specific — never vague
- Quantify everything with exact dollar amounts
- Prioritize by impact (biggest savings/gains first)
- Identify patterns the business owner might miss (recurring charges, growing expenses, declining revenue)
- Flag "ant expenses" (gastos hormiga) — small recurring charges that accumulate
- Compare revenue vs expenses to assess profitability
- Give a financial health score (1-100) with specific reasoning

Output format (JSON):
{{
  "health_score": 75,
  "health_summary": "One paragraph assessment",
  "total_revenue_usd": 0,
  "total_expenses_usd": 0,
  "net_profit_usd": 0,
  "critical_alerts": [
    {{"severity": "high|medium|low", "title": "...", "description": "...", "action": "...", "potential_savings": 0}}
  ],
  "ant_expenses": [
    {{"vendor": "...", "monthly_cost": 0, "occurrences": 0, "recommendation": "..."}}
  ],
  "revenue_insights": [
    {{"company": "...", "insight": "...", "recommendation": "..."}}
  ],
  "cost_optimization": [
    {{"category": "...", "current_spend": 0, "recommendation": "...", "potential_savings": 0}}
  ],
  "top_3_actions": [
    {{"priority": 1, "action": "...", "expected_impact": "..."}}
  ]
}}"""

    user_message = f"""Here is the complete financial snapshot for the last {snapshot['period_days']} days:

{json.dumps(snapshot, indent=2, default=str)}

Analyze this data thoroughly and provide your insights in the JSON format specified."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )
            if r.status_code != 200:
                logger.error("anthropic_error status=%s body=%s", r.status_code, r.text[:300])
                return {"error": f"AI API error: {r.status_code}"}

            response = r.json()
            content = response.get("content", [{}])[0].get("text", "")

            # Parse JSON from response (handle markdown code blocks)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            try:
                insights = json.loads(content)
            except json.JSONDecodeError:
                insights = {"raw_analysis": content}

            return insights

    except Exception as e:
        logger.error("ai_insights_error err=%s", str(e)[:200])
        return {"error": str(e)[:200]}
