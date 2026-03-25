"""
Finance Dashboard Smoke Tests — FROZEN CONTRACT
================================================
These tests verify the LIVE API returns correct data structures.
Run before ANY deploy: pytest tests/test_finance_smoke.py -v

If any test fails, DO NOT DEPLOY. Fix the issue first.
"""
import os
import requests
import pytest

API = os.getenv("API_URL", "https://calls-mx.fly.dev")
HEADERS = {"x-spartans-key": "superadmin_2clicks_finance_key_2026"}


class TestOverview:
    """Frozen contract for GET /v1/finance/overview"""

    @pytest.fixture(autouse=True, scope="class")
    def fetch(self, request):
        r = requests.get(f"{API}/v1/finance/overview", headers=HEADERS, timeout=30)
        assert r.status_code == 200
        request.cls.data = r.json()["data"]

    def test_stripe_accounts_exist(self):
        stripe = self.data.get("stripe", {})
        assert len(stripe) >= 3
        for key in ["lba", "uvul", "2clicks"]:
            assert key in stripe

    def test_stripe_has_balance(self):
        for key, acct in self.data["stripe"].items():
            for field in ["available", "pending", "currency", "name"]:
                assert field in acct

    def test_mercury_accounts_exist(self):
        assert len(self.data.get("mercury", {})) >= 2

    def test_mercury_has_balance(self):
        for key, acct in self.data["mercury"].items():
            assert "balance" in acct and "accounts" in acct

    def test_whop_connected_with_balance(self):
        whop = self.data.get("whop")
        assert whop is not None and whop.get("connected") is True
        bal = whop.get("balance")
        assert bal is not None
        for field in ["total", "available", "pending", "reserved"]:
            assert field in bal

    def test_whop_has_revenue(self):
        whop = self.data["whop"]
        assert "revenue_30d" in whop and "payments_30d" in whop

    def test_totals_correct(self):
        totals = self.data.get("totals", {})
        assert totals.get("usd", 0) > 100000
        assert "mxn" in totals
        # Verify math: Stripe USD + Mercury + Whop = total
        stripe_usd = sum(
            a["available"] + a["pending"]
            for a in self.data["stripe"].values() if a["currency"] == "USD"
        )
        mercury = sum(a["balance"] for a in self.data["mercury"].values())
        whop = self.data["whop"]["balance"]["total"]
        assert abs((stripe_usd + mercury + whop) - totals["usd"]) < 1


class TestRevenue:
    """Frozen contract for GET /v1/finance/revenue"""

    @pytest.fixture(autouse=True, scope="class")
    def fetch(self, request):
        r = requests.get(
            f"{API}/v1/finance/revenue", headers=HEADERS,
            params={"days": 30, "period": "day"}, timeout=60,
        )
        assert r.status_code == 200
        request.cls.data = r.json()["data"]

    def test_summary_structure(self):
        s = self.data["summary"]
        assert s["total_usd"] > 50000
        assert "total_mxn" in s and "count" in s

    def test_periods_structure(self):
        p = self.data["periods"][0]
        for field in ["period", "total_usd", "total_mxn", "by_source"]:
            assert field in p

    def test_pagination_works(self):
        """Must have 500+ txns — if less, Stripe pagination is broken."""
        assert len(self.data["transactions"]) > 500

    def test_whop_included_as_lba(self):
        whop_txns = [t for t in self.data["transactions"]
                     if "Whop" in (t.get("description") or "")]
        assert len(whop_txns) > 0
        assert all(t["source"] == "stripe_lba" for t in whop_txns)

    def test_mxn_usd_both_present(self):
        currencies = set(t["currency"] for t in self.data["transactions"])
        assert "USD" in currencies and "MXN" in currencies


class TestExpenses:
    """Frozen contract for GET /v1/finance/expenses"""

    @pytest.fixture(autouse=True, scope="class")
    def fetch(self, request):
        r = requests.get(
            f"{API}/v1/finance/expenses", headers=HEADERS,
            params={"days": 30}, timeout=60,
        )
        assert r.status_code == 200
        request.cls.data = r.json()["data"]

    def test_summary_structure(self):
        s = self.data["summary"]
        assert "total_income" in s and "total_expense" in s

    def test_has_transactions(self):
        assert len(self.data["transactions"]) > 0
        t = self.data["transactions"][0]
        for field in ["source", "source_name", "amount", "date"]:
            assert field in t
