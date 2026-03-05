"""Google Sheets integration for lead sync.

Sheet 1 — All Leads (real-time backup):
    Every lead is synced when created or updated.
    Triggered by whatsapp.py and payments.py via fire-and-forget asyncio tasks.

Sheet 2 — Sales Leads for Spartans:
    Leads who did NOT buy VIP after 1 hour of registration.
    Synced by the automation cron (every 5 min).
    Sellers (Spartans) call these leads to try to sell VIP upgrades.

Threading: gspread is synchronous. All calls run inside
anyio.to_thread.run_sync() to avoid blocking the event loop.

Auth: Google Service Account JSON stored in GOOGLE_SERVICE_ACCOUNT_JSON env var.
If not configured, all functions silently no-op (graceful degradation).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anyio

from ..settings import settings

logger = logging.getLogger("google_sheets")

# Scopes needed for read/write access to Google Sheets
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Module-level cached client (lazy init)
_client = None

# ---------------------------------------------------------------------------
# Column layouts
# ---------------------------------------------------------------------------

ALL_LEADS_COLUMNS = [
    "lead_id", "name", "email", "whatsapp", "phone",
    "status", "payment_status", "event_id",
    "last_contact_at",
]

SALES_LEADS_COLUMNS = [
    "lead_id", "name", "email", "whatsapp", "phone",
    "status", "event_id", "last_contact_at",
]

# ---------------------------------------------------------------------------
# Auth / client
# ---------------------------------------------------------------------------


def _get_client():
    """Lazy-init and cache a gspread Client from service account creds.

    Returns None if credentials are missing or invalid.
    """
    global _client
    if _client is not None:
        return _client

    raw = settings.google_service_account_json
    if not raw:
        logger.debug("google_sheets_disabled: GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.error("google_sheets_import_failed: gspread or google-auth not installed")
        return None

    # Parse credentials: try raw JSON first, then base64
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e1:
        try:
            info = json.loads(base64.b64decode(raw))
        except Exception:
            logger.error(
                "google_sheets_creds_invalid: cannot parse service account JSON "
                "json_err=%s raw_len=%d raw_start=%s",
                str(e1)[:100], len(raw), repr(raw[:60]),
            )
            return None

    try:
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        _client = gspread.authorize(creds)
        logger.info("google_sheets_client_initialized")
        return _client
    except Exception as exc:
        logger.error("google_sheets_auth_failed: %s", str(exc)[:300])
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lead_to_all_leads_row(lead: dict[str, Any]) -> list[str]:
    """Convert lead dict → row values for the All Leads sheet."""
    return [str(lead.get(col) or "") for col in ALL_LEADS_COLUMNS]


def _lead_to_sales_row(lead: dict[str, Any]) -> list[str]:
    """Convert lead dict → row values for the Sales Leads sheet."""
    row = [str(lead.get(col) or "") for col in SALES_LEADS_COLUMNS]
    # Add seller columns: call_status (PENDIENTE) and notes (empty)
    row.extend(["PENDIENTE", ""])
    return row


def _col_letter(n: int) -> str:
    """Convert 1-based column index to letter (1=A, 2=B, ..., 26=Z)."""
    return chr(64 + n)


def _ensure_headers_sync(ws, columns: list[str]) -> None:
    """Set header row if the sheet is empty or missing headers."""
    try:
        existing = ws.row_values(1)
        if existing and existing[0] == columns[0]:
            return  # Headers already set
    except Exception:
        pass

    try:
        ws.update(f"A1:{_col_letter(len(columns))}1", [columns])
        logger.info("gsheet_headers_set cols=%d", len(columns))
    except Exception as exc:
        logger.warning("gsheet_headers_failed err=%s", str(exc)[:200])


# ---------------------------------------------------------------------------
# Sheet 1: All Leads (sync)
# ---------------------------------------------------------------------------


def _sync_lead_to_all_leads_sync(lead: dict[str, Any]) -> None:
    """Sync a single lead to the All Leads sheet (synchronous)."""
    client = _get_client()
    if not client or not settings.gsheet_all_leads_id:
        return

    try:
        import gspread
    except ImportError:
        return

    try:
        sh = client.open_by_key(settings.gsheet_all_leads_id)
        ws = sh.sheet1
    except Exception as exc:
        logger.error("gsheet_open_failed sheet=all_leads err=%s", str(exc)[:200])
        return

    # Ensure headers on first use
    _ensure_headers_sync(ws, ALL_LEADS_COLUMNS)

    lead_id = str(lead.get("lead_id") or "")
    if not lead_id:
        return

    row_data = _lead_to_all_leads_row(lead)

    wa = str(lead.get("whatsapp") or "").strip()

    try:
        # Find existing row by lead_id in column A
        cell = None
        try:
            cell = ws.find(lead_id, in_column=1)
        except gspread.exceptions.CellNotFound:
            pass

        # Fallback: find by WhatsApp in column D (dedup by phone number)
        if not cell and wa:
            try:
                cell = ws.find(wa, in_column=4)
            except gspread.exceptions.CellNotFound:
                pass

        if cell:
            # Update existing row (also updates lead_id if it changed)
            end_col = _col_letter(len(row_data))
            ws.update(f"A{cell.row}:{end_col}{cell.row}", [row_data])
            logger.info("gsheet_updated sheet=all_leads lead=%s row=%d", lead_id, cell.row)
        else:
            ws.append_row(row_data, value_input_option="RAW")
            logger.info("gsheet_appended sheet=all_leads lead=%s", lead_id)
    except Exception as exc:
        logger.error("gsheet_sync_failed sheet=all_leads lead=%s err=%s", lead_id, str(exc)[:200])


async def sync_lead_to_all_leads_sheet(lead: dict[str, Any]) -> None:
    """Fire-and-forget: sync a lead to the All Leads Google Sheet.

    Safe to call from asyncio.create_task(). Never raises.
    """
    try:
        await anyio.to_thread.run_sync(lambda: _sync_lead_to_all_leads_sync(lead))
    except Exception as exc:
        logger.error("gsheet_async_failed sheet=all_leads err=%s", str(exc)[:200])


# ---------------------------------------------------------------------------
# Sheet 2: Sales Leads for Spartans
# ---------------------------------------------------------------------------


def _sync_sales_leads_sync(eligible_leads: list[dict[str, Any]]) -> dict[str, int]:
    """Sync eligible sales leads to the Spartans sheet (synchronous).

    Only appends NEW leads (skips existing by lead_id).
    Never overwrites seller columns (call_status, notes).
    """
    client = _get_client()
    if not client or not settings.gsheet_sales_leads_id:
        return {"synced": 0, "total_eligible": len(eligible_leads)}

    try:
        import gspread
    except ImportError:
        return {"synced": 0, "total_eligible": len(eligible_leads)}

    try:
        sh = client.open_by_key(settings.gsheet_sales_leads_id)
        ws = sh.sheet1
    except Exception as exc:
        logger.error("gsheet_open_failed sheet=sales_leads err=%s", str(exc)[:200])
        return {"synced": 0, "total_eligible": len(eligible_leads)}

    # Ensure headers (includes seller columns)
    headers = SALES_LEADS_COLUMNS + ["call_status", "notes"]
    _ensure_headers_sync(ws, headers)

    # Get existing lead_ids (col A) and WhatsApp numbers (col D) to dedup
    try:
        existing_ids = set(ws.col_values(1)[1:])
    except Exception:
        existing_ids = set()
    try:
        existing_wa = set(ws.col_values(4)[1:])
    except Exception:
        existing_wa = set()

    synced = 0
    for lead in eligible_leads:
        lead_id = str(lead.get("lead_id") or "")
        wa = str(lead.get("whatsapp") or "").strip()
        # Skip if already in sheet (by lead_id OR by WhatsApp number)
        if not lead_id or lead_id in existing_ids:
            continue
        if wa and wa in existing_wa:
            continue

        row = _lead_to_sales_row(lead)

        try:
            ws.append_row(row, value_input_option="RAW")
            synced += 1
        except Exception as exc:
            logger.error(
                "gsheet_append_failed sheet=sales_leads lead=%s err=%s",
                lead_id, str(exc)[:200],
            )

    if synced:
        logger.info("gsheet_sales_sync synced=%d total=%d", synced, len(eligible_leads))
    return {"synced": synced, "total_eligible": len(eligible_leads)}


async def sync_sales_leads_sheet() -> dict[str, int]:
    """Sync eligible sales leads to the Spartans Google Sheet.

    Called by the automation cron. Queries Supabase for leads who:
    - Have a ticket (GENERAL_CONFIRMED, VIP_INTERESTED, VIP_LINK_SENT)
    - Did NOT pay for VIP (payment_status != PAID)
    - Were created more than 1 hour ago
    - Are not marked do_not_contact

    Returns stats dict: {synced, total_eligible}.
    """
    from ..deps import sb

    now = datetime.now(timezone.utc)
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()

    try:
        r = (
            sb.table("leads")
            .select("*")
            .in_("status", ["GENERAL_CONFIRMED", "VIP_INTERESTED", "VIP_LINK_SENT"])
            .neq("payment_status", "PAID")
            .eq("do_not_contact", False)
            .lt("last_contact_at", thirty_min_ago)
            .execute()
        )
        eligible = r.data or []
    except Exception as exc:
        logger.error("gsheet_sales_query_failed err=%s", str(exc)[:200])
        return {"synced": 0, "total_eligible": 0}

    # Filter: must have a non-empty whatsapp or phone
    eligible = [
        l for l in eligible
        if (l.get("whatsapp") or l.get("phone") or "").strip()
    ]

    if not eligible:
        return {"synced": 0, "total_eligible": 0}

    try:
        return await anyio.to_thread.run_sync(lambda: _sync_sales_leads_sync(eligible))
    except Exception as exc:
        logger.error("gsheet_sales_async_failed err=%s", str(exc)[:200])
        return {"synced": 0, "total_eligible": len(eligible)}
