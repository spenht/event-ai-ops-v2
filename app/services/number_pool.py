"""Number Pool Manager — smart phone number rotation for outbound calls.

Manages a pool of phone numbers per campaign with:
- Round-robin weighted selection (least used + best health)
- Answer rate tracking per number
- Automatic cooling/flagging of unhealthy numbers
- Telnyx API integration for purchase/release/CNAM
- Backward compatible: falls back to campaign.telnyx_from_number
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..deps import sb
from ..settings import settings

logger = logging.getLogger("number_pool")

TELNYX_API_BASE = "https://api.telnyx.com/v2"

# ─── Health thresholds ──────────────────────────────────────────────────────

MIN_CALLS_FOR_RATING = 10      # Don't judge a number until it has this many calls
ANSWER_RATE_COOLING = 15.0     # Below this % → cooling
CONSECUTIVE_FAIL_LIMIT = 10    # This many failures in a row → cooling
WARMING_HOURS = 48             # Hours before warming → active
COOLING_HOURS = 24             # Hours in cooling before → flagged
FLAGGED_DAYS = 7               # Days in flagged before → retired

# Phone prefix → country code mapping
_COUNTRY_PREFIXES = {
    "+1": "US",  # US/CA (simplified)
    "+52": "MX",
    "+44": "GB",
    "+57": "CO",
    "+34": "ES",
    "+49": "DE",
    "+33": "FR",
    "+55": "BR",
    "+56": "CL",
    "+54": "AR",
    "+51": "PE",
    "+593": "EC",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _telnyx_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _resolve_api_key(campaign: dict | None) -> str:
    """Get Telnyx API key from campaign or global settings."""
    key = ""
    if campaign:
        key = (campaign.get("telnyx_api_key") or "").strip()
    return key or settings.telnyx_api_key


def detect_lead_country(phone: str) -> str:
    """Detect country code from phone number prefix.

    Returns 2-letter country code (e.g. "US", "MX").
    """
    phone = phone.strip()
    # Check longest prefixes first
    for prefix in sorted(_COUNTRY_PREFIXES.keys(), key=len, reverse=True):
        if phone.startswith(prefix):
            return _COUNTRY_PREFIXES[prefix]
    return "US"  # Default


def _pool_enabled(campaign: dict | None) -> bool:
    """Check if number pool is enabled for this campaign."""
    if not campaign:
        return False
    config = campaign.get("number_pool_config") or {}
    if isinstance(config, str):
        import json
        try:
            config = json.loads(config)
        except Exception:
            return False
    return bool(config.get("enabled", False))


# ─── Core: Pick a number from the pool ─────────────────────────────────────


async def pick_number(
    campaign_id: str,
    campaign: dict | None = None,
    country: str = "US",
) -> str:
    """Select the best available number from the pool.

    Strategy: round-robin weighted by health.
    1. Find numbers with status='active' for this campaign + country
    2. Filter out numbers in cooldown or at max_calls_per_day
    3. Order by: fewest calls today (round-robin) + best answer_rate
    4. If no pool numbers → fallback to campaign.telnyx_from_number

    Returns: phone number string (E.164) or empty string.
    """
    # Check if pool is enabled
    if not _pool_enabled(campaign):
        # Fallback to legacy single number
        if campaign:
            return (campaign.get("telnyx_from_number") or "").strip()
        return settings.telnyx_from_number

    now = datetime.now(timezone.utc).isoformat()

    try:
        # Query active numbers for this campaign + country
        # Try with health columns first (after migration 011), fallback to basic
        try:
            query = (
                sb.table("phone_numbers")
                .select("*")
                .eq("campaign_id", campaign_id)
                .eq("status", "active")
                .order("calls_today", desc=False)   # Least used first (round-robin)
                .order("answer_rate", desc=True)     # Best answer rate as tiebreaker
            )
            if country:
                query = query.eq("country", country)
            r = query.execute()
            numbers = r.data or []
        except Exception:
            # Fallback: basic query without health columns
            query = (
                sb.table("phone_numbers")
                .select("*")
                .eq("campaign_id", campaign_id)
                .eq("status", "active")
                .order("created_at", desc=False)
            )
            if country:
                query = query.eq("country", country)
            r = query.execute()
            numbers = r.data or []
    except Exception as exc:
        logger.error(
            "pick_number_query_failed campaign=%s err=%s",
            campaign_id,
            str(exc)[:300],
        )
        numbers = []

    # Filter out numbers in cooldown or at max daily limit
    available = []
    for num in numbers:
        # Skip if in cooldown
        cooldown_until = num.get("cooldown_until")
        if cooldown_until:
            try:
                cd = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
                if cd > datetime.now(timezone.utc):
                    continue
            except Exception:
                pass

        # Skip if at max calls per day
        calls_today = num.get("calls_today", 0)
        max_per_day = num.get("max_calls_per_day", 50)
        if calls_today >= max_per_day:
            continue

        available.append(num)

    if available:
        chosen = available[0]
        logger.info(
            "pick_number campaign=%s chosen=%s calls_today=%d ar=%.1f%%",
            campaign_id,
            chosen["number"],
            chosen.get("calls_today", 0),
            chosen.get("answer_rate", 0),
        )
        return chosen["number"]

    # No pool numbers in this country — try without country filter
    if country != "US":
        logger.warning(
            "pick_number_no_%s_numbers campaign=%s falling_back_to_US",
            country,
            campaign_id,
        )
        return await pick_number(campaign_id, campaign, country="US")

    # Absolute fallback: campaign's legacy from_number
    fallback = ""
    if campaign:
        fallback = (campaign.get("telnyx_from_number") or "").strip()
    if not fallback:
        fallback = settings.telnyx_from_number

    if fallback:
        logger.warning(
            "pick_number_pool_empty campaign=%s using_fallback=%s",
            campaign_id,
            fallback,
        )
    else:
        logger.error("pick_number_no_number campaign=%s", campaign_id)

    return fallback


# ─── Core: Record call result ──────────────────────────────────────────────


async def record_call_result(
    from_number: str,
    campaign_id: str,
    result: str,
) -> None:
    """Update number health stats after a call completes.

    result: "answered", "no_answer", "busy", "failed", "voicemail"
    """
    if not from_number:
        return

    try:
        # Find the number in pool
        r = (
            sb.table("phone_numbers")
            .select("id, total_calls, answered_calls, answer_rate, consecutive_failures, calls_today, status")
            .eq("number", from_number)
            .eq("campaign_id", campaign_id)
            .neq("status", "retired")
            .limit(1)
            .execute()
        )
        num = (r.data or [None])[0]
        if not num:
            # Number not in pool (might be legacy single number) — ignore
            return

        now = datetime.now(timezone.utc).isoformat()
        total = (num.get("total_calls") or 0) + 1
        answered = num.get("answered_calls") or 0
        consec_fail = num.get("consecutive_failures") or 0
        calls_today = (num.get("calls_today") or 0) + 1
        current_status = num.get("status", "active")

        if result == "answered":
            answered += 1
            consec_fail = 0  # Reset on success
        elif result in ("no_answer", "busy", "failed"):
            consec_fail += 1
        # "voicemail" — increment total but don't count as failure or success

        answer_rate = (answered / total * 100) if total > 0 else 0

        update_data: dict[str, Any] = {
            "total_calls": total,
            "answered_calls": answered,
            "answer_rate": round(answer_rate, 1),
            "consecutive_failures": consec_fail,
            "calls_today": calls_today,
            "last_used_at": now,
            "updated_at": now,
        }

        # Auto-cooling: flag unhealthy numbers
        if current_status == "active":
            should_cool = False
            if consec_fail >= CONSECUTIVE_FAIL_LIMIT:
                should_cool = True
                logger.warning(
                    "number_auto_cooling_consec_fail number=%s fails=%d",
                    from_number,
                    consec_fail,
                )
            elif total >= MIN_CALLS_FOR_RATING and answer_rate < ANSWER_RATE_COOLING:
                should_cool = True
                logger.warning(
                    "number_auto_cooling_low_ar number=%s ar=%.1f%% calls=%d",
                    from_number,
                    answer_rate,
                    total,
                )

            if should_cool:
                update_data["status"] = "cooling"
                update_data["cooldown_until"] = (
                    datetime.now(timezone.utc) + timedelta(hours=COOLING_HOURS)
                ).isoformat()

        sb.table("phone_numbers").update(update_data).eq("id", num["id"]).execute()

        logger.debug(
            "record_call_result number=%s result=%s total=%d answered=%d ar=%.1f%%",
            from_number,
            result,
            total,
            answered,
            answer_rate,
        )

    except Exception as exc:
        logger.error(
            "record_call_result_failed number=%s err=%s",
            from_number,
            str(exc)[:300],
        )


# ─── Health check (cron) ───────────────────────────────────────────────────


async def check_pool_health(campaign_id: str | None = None) -> dict:
    """Review health of all numbers (or for a specific campaign).

    State transitions:
    - warming + created > WARMING_HOURS ago → promote to 'active'
    - cooling + cooldown_until passed + still bad → 'flagged'
    - cooling + cooldown_until passed + recovered → back to 'active'
    - flagged + FLAGGED_DAYS → 'retired'
    - Reset calls_today if it's a new day
    """
    stats = {"checked": 0, "promoted": 0, "cooled": 0, "flagged": 0, "retired": 0, "reset_daily": 0}
    now = datetime.now(timezone.utc)

    try:
        query = (
            sb.table("phone_numbers")
            .select("*")
            .neq("status", "retired")
        )
        if campaign_id:
            query = query.eq("campaign_id", campaign_id)

        r = query.execute()
        numbers = r.data or []
    except Exception as exc:
        logger.error("check_pool_health_fetch err=%s", str(exc)[:300])
        return stats

    for num in numbers:
        stats["checked"] += 1
        num_id = num["id"]
        status = num.get("status", "active")
        updates: dict[str, Any] = {}

        # Reset calls_today if last_used_at was yesterday or earlier
        last_used = num.get("last_used_at")
        if last_used:
            try:
                lu = datetime.fromisoformat(str(last_used).replace("Z", "+00:00"))
                if lu.date() < now.date():
                    updates["calls_today"] = 0
                    stats["reset_daily"] += 1
            except Exception:
                pass

        # Warming → Active
        if status == "warming":
            created = num.get("created_at", "")
            try:
                ct = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if (now - ct).total_seconds() > WARMING_HOURS * 3600:
                    updates["status"] = "active"
                    stats["promoted"] += 1
                    logger.info("number_promoted number=%s", num["number"])
            except Exception:
                pass

        # Cooling → Flagged or back to Active
        elif status == "cooling":
            cooldown_until = num.get("cooldown_until")
            if cooldown_until:
                try:
                    cd = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
                    if now > cd:
                        ar = num.get("answer_rate", 0)
                        if ar < ANSWER_RATE_COOLING and (num.get("total_calls") or 0) >= MIN_CALLS_FOR_RATING:
                            updates["status"] = "flagged"
                            updates["flagged_at"] = now.isoformat()
                            stats["flagged"] += 1
                            logger.warning(
                                "number_flagged number=%s ar=%.1f%%",
                                num["number"],
                                ar,
                            )
                        else:
                            # Answer rate recovered or not enough calls — give it another chance
                            updates["status"] = "active"
                            updates["consecutive_failures"] = 0
                            stats["promoted"] += 1
                            logger.info(
                                "number_recovered number=%s ar=%.1f%%",
                                num["number"],
                                ar,
                            )
                except Exception:
                    pass

        # Flagged → Retired
        elif status == "flagged":
            flagged_at = num.get("flagged_at")
            if flagged_at:
                try:
                    fa = datetime.fromisoformat(str(flagged_at).replace("Z", "+00:00"))
                    if (now - fa).days >= FLAGGED_DAYS:
                        updates["status"] = "retired"
                        updates["retired_at"] = now.isoformat()
                        stats["retired"] += 1
                        logger.info("number_retired number=%s", num["number"])
                except Exception:
                    pass

        # Apply updates
        if updates:
            updates["updated_at"] = now.isoformat()
            try:
                sb.table("phone_numbers").update(updates).eq("id", num_id).execute()
            except Exception as exc:
                logger.error(
                    "health_update_failed number=%s err=%s",
                    num.get("number", ""),
                    str(exc)[:200],
                )

    logger.info(
        "pool_health_check checked=%d promoted=%d flagged=%d retired=%d reset=%d",
        stats["checked"],
        stats["promoted"],
        stats["flagged"],
        stats["retired"],
        stats["reset_daily"],
    )
    return stats


# ─── Auto-replenish pools ──────────────────────────────────────────────────


async def auto_replenish_pools() -> dict:
    """For each campaign with auto_purchase=true, buy numbers if below min_active.

    Returns: {"campaigns_checked": N, "numbers_purchased": N}
    """
    result = {"campaigns_checked": 0, "numbers_purchased": 0}

    try:
        cr = (
            sb.table("campaigns")
            .select("id, org_id, number_pool_config, telnyx_api_key, telnyx_sip_connection_id")
            .eq("status", "active")
            .execute()
        )
        campaigns = cr.data or []
    except Exception as exc:
        logger.error("auto_replenish_campaign_fetch err=%s", str(exc)[:300])
        return result

    for camp in campaigns:
        config = camp.get("number_pool_config") or {}
        if isinstance(config, str):
            import json
            try:
                config = json.loads(config)
            except Exception:
                continue

        if not config.get("enabled") or not config.get("auto_purchase"):
            continue

        result["campaigns_checked"] += 1
        campaign_id = camp["id"]
        min_active = config.get("min_active", 2)
        country = config.get("auto_purchase_country", "US")

        # Count active numbers
        try:
            nr = (
                sb.table("phone_numbers")
                .select("id", count="exact")
                .eq("campaign_id", campaign_id)
                .eq("status", "active")
                .execute()
            )
            active_count = nr.count or 0
        except Exception as exc:
            logger.error(
                "auto_replenish_count_failed campaign=%s err=%s",
                campaign_id,
                str(exc)[:200],
            )
            continue

        if active_count >= min_active:
            continue

        # Need to buy (min_active - active_count) numbers
        to_buy = min_active - active_count
        logger.info(
            "auto_replenish campaign=%s active=%d min=%d buying=%d",
            campaign_id,
            active_count,
            min_active,
            to_buy,
        )

        for _ in range(to_buy):
            try:
                purchased = await purchase_number(
                    campaign_id=campaign_id,
                    org_id=camp.get("org_id", ""),
                    campaign=camp,
                    country=country,
                )
                if purchased:
                    result["numbers_purchased"] += 1
            except Exception as exc:
                logger.error(
                    "auto_replenish_purchase_failed campaign=%s err=%s",
                    campaign_id,
                    str(exc)[:300],
                )
                break  # Stop buying if one fails

    logger.info(
        "auto_replenish done campaigns=%d purchased=%d",
        result["campaigns_checked"],
        result["numbers_purchased"],
    )
    return result


# ─── Telnyx: Import existing numbers ───────────────────────────────────────


async def import_existing_numbers(
    campaign_id: str,
    org_id: str,
    campaign: dict | None = None,
) -> list[dict]:
    """Import existing Telnyx numbers into the phone_numbers pool.

    Fetches all numbers from Telnyx API and inserts any that aren't already in DB.
    """
    api_key = _resolve_api_key(campaign)
    if not api_key:
        logger.error("import_numbers_no_api_key campaign=%s", campaign_id)
        return []

    imported = []

    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                f"{TELNYX_API_BASE}/phone_numbers?page%5Bsize%5D=50",
                headers=_telnyx_headers(api_key),
            )
            r.raise_for_status()
            telnyx_numbers = r.json().get("data", [])
    except Exception as exc:
        logger.error("import_numbers_telnyx_fetch err=%s", str(exc)[:300])
        return []

    for tn in telnyx_numbers:
        phone = tn.get("phone_number", "")
        telnyx_id = tn.get("id", "")
        if not phone:
            continue

        # Check if already in DB
        try:
            existing = (
                sb.table("phone_numbers")
                .select("id")
                .eq("number", phone)
                .eq("campaign_id", campaign_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                continue  # Already imported
        except Exception:
            pass

        # Detect country
        country = detect_lead_country(phone)

        # Insert
        try:
            row = {
                "campaign_id": campaign_id,
                "org_id": org_id,
                "number": phone,
                "country": country,
                "provider": "telnyx",
                "provider_id": telnyx_id,
                "telnyx_number_id": telnyx_id,
                "type": "local",
                "status": "active",  # Existing numbers are already warm
                "max_calls_per_day": 50,
            }
            ir = sb.table("phone_numbers").insert(row).execute()
            if ir.data:
                imported.append(ir.data[0])
                logger.info("imported_number %s for campaign %s", phone, campaign_id)
        except Exception as exc:
            logger.error(
                "import_number_insert_failed number=%s err=%s",
                phone,
                str(exc)[:200],
            )

    logger.info(
        "import_numbers done campaign=%s telnyx=%d imported=%d",
        campaign_id,
        len(telnyx_numbers),
        len(imported),
    )
    return imported


# ─── Telnyx: List available numbers (for selective import) ────────────────


async def list_available_telnyx_numbers(
    campaign_id: str,
    campaign: dict | None = None,
) -> list[dict]:
    """List all Telnyx numbers with their current campaign assignment info.

    Returns a list of dicts:
      - phone_number, telnyx_id, country
      - assigned_campaign_id (null if not assigned to any campaign)
      - assigned_campaign_name (if assigned)
      - already_in_this_campaign (bool)
    """
    api_key = _resolve_api_key(campaign)
    if not api_key:
        logger.error("list_available_no_api_key campaign=%s", campaign_id)
        return []

    # 1. Fetch all numbers from Telnyx
    try:
        all_telnyx: list[dict] = []
        page = 1
        while True:
            with httpx.Client(timeout=20.0) as client:
                r = client.get(
                    f"{TELNYX_API_BASE}/phone_numbers",
                    params={"page[size]": "250", "page[number]": str(page)},
                    headers=_telnyx_headers(api_key),
                )
                r.raise_for_status()
                data = r.json().get("data", [])
                all_telnyx.extend(data)
                # Stop if fewer than page size (last page)
                if len(data) < 250:
                    break
                page += 1
    except Exception as exc:
        logger.error("list_available_telnyx_fetch err=%s", str(exc)[:300])
        return []

    # 2. Fetch all non-retired pool numbers across all campaigns (to know assignments)
    try:
        pr = (
            sb.table("phone_numbers")
            .select("number, campaign_id, status")
            .neq("status", "retired")
            .execute()
        )
        pool_rows = pr.data or []
    except Exception as exc:
        logger.error("list_available_pool_fetch err=%s", str(exc)[:300])
        pool_rows = []

    # Build lookup: phone_number -> campaign_id
    number_to_campaign: dict[str, str] = {}
    for row in pool_rows:
        number_to_campaign[row["number"]] = row["campaign_id"]

    # 3. Fetch campaign names for assigned numbers
    assigned_campaign_ids = set(number_to_campaign.values())
    campaign_names: dict[str, str] = {}
    if assigned_campaign_ids:
        try:
            cr = (
                sb.table("campaigns")
                .select("id, name")
                .in_("id", list(assigned_campaign_ids))
                .execute()
            )
            for c in cr.data or []:
                campaign_names[c["id"]] = c["name"]
        except Exception:
            pass

    # 4. Build result
    result = []
    for tn in all_telnyx:
        phone = tn.get("phone_number", "")
        if not phone:
            continue
        assigned_cid = number_to_campaign.get(phone)
        result.append({
            "phone_number": phone,
            "telnyx_id": tn.get("id", ""),
            "country": detect_lead_country(phone),
            "connection_name": tn.get("connection_name", ""),
            "assigned_campaign_id": assigned_cid,
            "assigned_campaign_name": campaign_names.get(assigned_cid, "") if assigned_cid else None,
            "already_in_this_campaign": assigned_cid == campaign_id,
        })

    logger.info(
        "list_available_telnyx done telnyx=%d assigned=%d",
        len(result),
        sum(1 for r in result if r["assigned_campaign_id"]),
    )
    return result


async def import_selected_numbers(
    campaign_id: str,
    org_id: str,
    telnyx_numbers: list[dict],
) -> list[dict]:
    """Import specific Telnyx numbers into the phone_numbers pool.

    telnyx_numbers: list of {phone_number, telnyx_id} dicts to import.
    """
    imported = []
    for tn in telnyx_numbers:
        phone = tn.get("phone_number", "")
        telnyx_id = tn.get("telnyx_id", "")
        if not phone:
            continue

        # Check if already in this campaign
        try:
            existing = (
                sb.table("phone_numbers")
                .select("id")
                .eq("number", phone)
                .eq("campaign_id", campaign_id)
                .neq("status", "retired")
                .limit(1)
                .execute()
            )
            if existing.data:
                continue
        except Exception:
            pass

        country = detect_lead_country(phone)
        try:
            row = {
                "campaign_id": campaign_id,
                "org_id": org_id,
                "number": phone,
                "country": country,
                "provider": "telnyx",
                "provider_id": telnyx_id,
                "telnyx_number_id": telnyx_id,
                "type": "local",
                "status": "active",
                "max_calls_per_day": 50,
            }
            ir = sb.table("phone_numbers").insert(row).execute()
            if ir.data:
                imported.append(ir.data[0])
                logger.info("imported_selected %s for campaign %s", phone, campaign_id)
        except Exception as exc:
            logger.error(
                "import_selected_insert_failed number=%s err=%s",
                phone,
                str(exc)[:200],
            )

    logger.info(
        "import_selected done campaign=%s requested=%d imported=%d",
        campaign_id,
        len(telnyx_numbers),
        len(imported),
    )
    return imported


# ─── Telnyx: Purchase a new number ─────────────────────────────────────────


async def purchase_number(
    campaign_id: str,
    org_id: str,
    campaign: dict | None = None,
    country: str = "US",
    area_code: str = "",
) -> dict | None:
    """Purchase a new phone number from Telnyx and add to pool.

    1. Search available numbers
    2. Order the first one with voice capability
    3. Assign to SIP connection
    4. Enable CNAM
    5. Insert into phone_numbers with status='warming'
    """
    api_key = _resolve_api_key(campaign)
    if not api_key:
        logger.error("purchase_number_no_api_key campaign=%s", campaign_id)
        return None

    connection_id = ""
    if campaign:
        connection_id = (
            (campaign.get("telnyx_sip_connection_id") or "").strip()
        )

    headers = _telnyx_headers(api_key)

    # 1. Search available numbers
    search_params = {
        "filter[country_code]": country,
        "filter[limit]": "5",
        "filter[features][]": "voice",
    }
    if area_code:
        search_params["filter[national_destination_code]"] = area_code

    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                f"{TELNYX_API_BASE}/available_phone_numbers",
                params=search_params,
                headers=headers,
            )
            r.raise_for_status()
            available = r.json().get("data", [])
    except Exception as exc:
        logger.error("purchase_search_failed country=%s err=%s", country, str(exc)[:300])
        return None

    if not available:
        logger.warning("purchase_no_numbers_available country=%s area=%s", country, area_code)
        return None

    chosen_number = available[0].get("phone_number", "")
    if not chosen_number:
        return None

    # 2. Order the number
    order_body: dict[str, Any] = {
        "phone_numbers": [{"phone_number": chosen_number}],
    }
    if connection_id:
        order_body["connection_id"] = connection_id

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{TELNYX_API_BASE}/number_orders",
                json=order_body,
                headers=headers,
            )
            r.raise_for_status()
            order_data = r.json().get("data", {})
            order_status = order_data.get("status", "")
            logger.info(
                "purchase_ordered number=%s status=%s",
                chosen_number,
                order_status,
            )
    except Exception as exc:
        logger.error(
            "purchase_order_failed number=%s err=%s",
            chosen_number,
            str(exc)[:300],
        )
        return None

    # 3. Get the Telnyx number ID (need to look it up after purchase)
    telnyx_number_id = ""
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                f"{TELNYX_API_BASE}/phone_numbers",
                params={"filter[phone_number]": chosen_number, "page[size]": "1"},
                headers=headers,
            )
            r.raise_for_status()
            nums = r.json().get("data", [])
            if nums:
                telnyx_number_id = nums[0].get("id", "")
    except Exception:
        pass

    # 4. Enable CNAM if we have the number ID
    cnam_name = ""
    if telnyx_number_id:
        try:
            cnam_name = "S Hoffmann"  # TODO: make configurable per campaign
            config = (campaign or {}).get("number_pool_config") or {}
            if isinstance(config, dict):
                cnam_name = config.get("cnam_name", cnam_name)

            with httpx.Client(timeout=20.0) as client:
                r = client.patch(
                    f"{TELNYX_API_BASE}/phone_numbers/{telnyx_number_id}/voice",
                    json={
                        "cnam_listing": {
                            "cnam_listing_enabled": True,
                            "cnam_listing_details": cnam_name,
                        }
                    },
                    headers=headers,
                )
                r.raise_for_status()
                logger.info("purchase_cnam_enabled number=%s name=%s", chosen_number, cnam_name)
        except Exception as exc:
            logger.warning(
                "purchase_cnam_failed number=%s err=%s",
                chosen_number,
                str(exc)[:200],
            )

    # 5. Insert into phone_numbers pool
    detected_country = detect_lead_country(chosen_number)
    row = {
        "campaign_id": campaign_id,
        "org_id": org_id,
        "number": chosen_number,
        "country": detected_country,
        "provider": "telnyx",
        "provider_id": telnyx_number_id,
        "telnyx_number_id": telnyx_number_id,
        "type": "local",
        "status": "warming",
        "max_calls_per_day": 50,
        "cnam_name": cnam_name,
        "cnam_registered": bool(cnam_name),
    }

    try:
        ir = sb.table("phone_numbers").insert(row).execute()
        inserted = (ir.data or [None])[0]
        logger.info(
            "purchase_complete number=%s campaign=%s status=warming",
            chosen_number,
            campaign_id,
        )
        return inserted
    except Exception as exc:
        logger.error(
            "purchase_insert_failed number=%s err=%s",
            chosen_number,
            str(exc)[:300],
        )
        return None


# ─── Telnyx: Release a number ──────────────────────────────────────────────


async def release_number(
    phone_number_id: str,
    campaign_id: str,
    campaign: dict | None = None,
) -> bool:
    """Release a number (return to Telnyx) and mark as retired."""
    api_key = _resolve_api_key(campaign)

    # Fetch the pool record
    try:
        r = (
            sb.table("phone_numbers")
            .select("*")
            .eq("id", phone_number_id)
            .eq("campaign_id", campaign_id)
            .limit(1)
            .execute()
        )
        num = (r.data or [None])[0]
        if not num:
            return False
    except Exception as exc:
        logger.error("release_fetch_failed id=%s err=%s", phone_number_id, str(exc)[:200])
        return False

    telnyx_id = num.get("telnyx_number_id", "")

    # Delete from Telnyx
    if telnyx_id and api_key:
        try:
            with httpx.Client(timeout=20.0) as client:
                r_del = client.delete(
                    f"{TELNYX_API_BASE}/phone_numbers/{telnyx_id}",
                    headers=_telnyx_headers(api_key),
                )
                if r_del.status_code < 400:
                    logger.info("release_telnyx_ok number=%s", num.get("number", ""))
                else:
                    logger.warning(
                        "release_telnyx_failed number=%s status=%d",
                        num.get("number", ""),
                        r_del.status_code,
                    )
        except Exception as exc:
            logger.warning(
                "release_telnyx_error number=%s err=%s",
                num.get("number", ""),
                str(exc)[:200],
            )

    # Mark as retired in DB
    now = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("phone_numbers").update({
            "status": "retired",
            "retired_at": now,
            "updated_at": now,
        }).eq("id", phone_number_id).execute()
        return True
    except Exception as exc:
        logger.error(
            "release_db_update_failed id=%s err=%s",
            phone_number_id,
            str(exc)[:200],
        )
        return False


# ─── Pool stats ─────────────────────────────────────────────────────────────


async def get_pool_stats(campaign_id: str) -> dict:
    """Get statistics for a campaign's number pool."""
    try:
        r = (
            sb.table("phone_numbers")
            .select("*")
            .eq("campaign_id", campaign_id)
            .neq("status", "retired")
            .execute()
        )
        numbers = r.data or []
    except Exception:
        return {"active": 0, "warming": 0, "cooling": 0, "flagged": 0}

    stats: dict[str, Any] = {
        "active": 0,
        "warming": 0,
        "cooling": 0,
        "flagged": 0,
        "total_numbers": len(numbers),
        "total_calls": 0,
        "total_answered": 0,
        "avg_answer_rate": 0,
    }

    for num in numbers:
        status = num.get("status", "active")
        if status in stats:
            stats[status] += 1
        stats["total_calls"] += num.get("total_calls", 0)
        stats["total_answered"] += num.get("answered_calls", 0)

    if stats["total_calls"] > 0:
        stats["avg_answer_rate"] = round(
            stats["total_answered"] / stats["total_calls"] * 100, 1
        )

    return stats
