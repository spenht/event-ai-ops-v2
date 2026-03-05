from __future__ import annotations

import asyncio
import logging
import re
import html
import uuid
import httpx
from typing import Any, Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from ..deps import sb
from ..settings import settings
from ..services.openai_chat import generate_reply, strip_tokens
from ..services.tickets import generate_ticket_png
from ..services.stripe_checkout import create_vip_checkout_link
from ..services.twilio_whatsapp import normalize_mx_whatsapp, send_whatsapp

logger = logging.getLogger("whatsapp")

router = APIRouter(prefix="/v1/messaging/whatsapp", tags=["whatsapp"])

@router.get("/media/{filename}")
async def whatsapp_media_proxy(filename: str):
    """Proxy public Supabase media so Twilio can fetch reliably."""
    base = "https://isfpcmgadtqzozkwztju.supabase.co/storage/v1/object/public/whatsapp/media/"
    url = base + filename

    async def _iter():
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(_iter(), media_type="video/mp4")

DEFAULT_SPEAKERS = (
    "Spencer Hoffmann, Daniel Marcos, Carlos Nava, Rafael Coppola, Millán Ludeña, "
    "Marcelo Gutiérrez, Florencia Montoya, Nara Trejo, Cesc López"
)


def _twiml_empty() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


# TwiML reply helper
def _twiml_message(body: str, media_urls: Optional[list[str]] = None) -> Response:
    """Return TwiML that replies to the inbound message.

    This avoids relying on an outbound Twilio REST call (which can fail independently) and
    works well for Twilio WhatsApp Sandbox and webhook-based replies.

    IMPORTANT: For WhatsApp media, Twilio is most reliable when the message text is in a
    <Body> element, and media are separate <Media> elements.
    """
    safe_body = html.escape((body or "").strip())
    media_urls = [u for u in (media_urls or []) if (u or "").startswith("https://")]

    media_xml = "".join(f"<Media>{html.escape(u)}</Media>" for u in media_urls)

    # Twilio expects <Response><Message><Body>..</Body><Media>..</Media></Message></Response>
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Message>"
        f"<Body>{safe_body}</Body>"
        f"{media_xml}"
        "</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


def _parse_twilio_form(raw: bytes) -> dict[str, str]:
    try:
        qs = parse_qs((raw or b"").decode("utf-8"), keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in qs.items()}
    except Exception:
        return {}



def _extract_email(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", text, re.I)
    return m.group(1).strip().lower() if m else None


# --- Phone extraction helper for companion ---
def _extract_phone_e164(text: str) -> Optional[str]:
    """Best-effort phone extraction in E.164 format.

    Accepts patterns like +521999..., +52999..., +1 (954) 756-4662, etc.
    Returns a compact +<digits> string.
    """
    if not text:
        return None

    # Prefer explicit +<digits>
    m = re.search(r"\+\s*\d[\d\s().-]{7,20}", text)
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        if 8 <= len(digits) <= 15:
            return "+" + digits

    # Fallback: 10-15 digits without plus (conservative)
    m2 = re.search(r"\b\d[\d\s().-]{9,18}\b", text)
    if m2:
        digits = re.sub(r"\D", "", m2.group(0))
        if 10 <= len(digits) <= 15:
            return "+" + digits

    return None


# --- Helper to strip media placeholders from AI output ---
def _strip_media_placeholders(text: str) -> str:
    if not text:
        return ""
    # Remove bracket placeholders like "[Video de Spencer]"
    t = re.sub(r"\n?\[[^\]]*video[^\]]*\]\n?", "\n", text, flags=re.I)
    # Remove lines like "Video de Spencer ..." without a real URL
    t = re.sub(r"\n?^\s*video\s+de\s+spencer[^\n]*$\n?", "\n", t, flags=re.I | re.M)
    # Remove common placeholder domains that the model might hallucinate
    t = re.sub(r"https?://(?:www\.)?linkdelvideo\.com\S*", "", t, flags=re.I)
    t = re.sub(r"https?://(?:www\.)?linkparapago\.com\S*", "", t, flags=re.I)
    # Remove any direct mp4 links (we send video via <Media>, not as plain text)
    t = re.sub(r"https?://\S+\.mp4\S*", "", t, flags=re.I)
    t = re.sub(r"\n?\(\s*\)\n?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

# --- Helper to extract name from a WhatsApp message (best-effort) ---
def _extract_name(text: str) -> Optional[str]:
    """Best-effort name extraction.

    IMPORTANT: Only accept explicit name-intent patterns to avoid false positives (e.g. "sí me encantaría").
    """
    if not text:
        return None

    t = re.sub(r"\s+", " ", text.strip())

    # Pattern: "Name, email" or "Name\nemail" (common when user provides both)
    email_in_text = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", t, re.I)
    if email_in_text:
        # Take everything before the email (or before comma/newline preceding it)
        pre = t[:email_in_text.start()].rstrip(" ,\n\t")
        # Clean up: remove common labels
        pre = re.sub(r"^(nombre|name)\s*[:\-]\s*", "", pre, flags=re.I).strip()
        pre = re.sub(r"^(correo|email|mail)\s*[:\-]\s*.*$", "", pre, flags=re.I | re.M).strip()
        if pre:
            words = pre.split()
            if 1 <= len(words) <= 5 and re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóüñ\s'.-]+", pre):
                name = pre
                # Skip the rejection logic below — this is a strong signal
                if len(name) >= 2:
                    return name[:80]

    # Common patterns: "soy Juan", "me llamo Juan", "mi nombre es Juan"
    m = re.search(
        r"\b(soy|me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÜÑáéíóüñ][A-Za-zÁÉÍÓÚÜÑáéíóüñ\s'.-]{1,60})",
        t,
        re.I,
    )
    if m:
        name = m.group(2).strip()
    else:
        # Explicit label: "Nombre: Juan Perez"
        m2 = re.search(
            r"\b(nombre)\s*[:\-]\s*([A-Za-zÁÉÍÓÚÜÑáéíóüñ][A-Za-zÁÉÍÓÚÜÑáéíóüñ\s'.-]{1,60})",
            t,
            re.I,
        )
        if m2:
            name = m2.group(2).strip()
        else:
            return None

    name = re.sub(r"\s+", " ", name).strip()

    # Reject if it looks like a sentence rather than a name
    low = name.lower()
    if any(
        w in low.split()
        for w in [
            "quiero",
            "encantaría",
            "encantaria",
            "asistir",
            "confirmo",
            "si",
            "sí",
            "claro",
            "gracias",
            "pago",
            "pagué",
            "pague",
            "pagado",
            "pagar",
            "vip",
            "general",
        ]
    ):
        return None

    if len(name) < 2:
        return None

    # Cap length
    return name[:80]


# --- Helper: detect "name-only" messages (conservative) ---
def _looks_like_name_only(text: str) -> Optional[str]:
    """If the user sends only their name (e.g., "Florencia Montoya"), capture it.

    We keep this conservative to avoid false positives.
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    if len(t) < 3 or len(t) > 80:
        return None
    if "@" in t:
        return None
    if re.search(r"\d", t):
        return None
    # 2-4 words, mostly letters (allow accents)
    parts = [p for p in t.split(" ") if p]
    if len(parts) < 2 or len(parts) > 4:
        return None
    if not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóüñ\s'.-]+", t):
        return None
    low = t.lower()
    # Avoid common conversational phrases
    banned = [
        "hola",
        "gracias",
        "ok",
        "sí",
        "si",
        "quiero",
        "asistir",
        "confirmo",
        "vip",
        "general",
        "ya",
        "pago",
        "pagué",
        "pague",
        "pagado",
        "pagar",
    ]
    if any(b in low.split() for b in banned):
        return None
    return t


def _mx_variants(e164: str) -> list[str]:
    if not e164:
        return [""]
    if e164.startswith("+521"):
        return [e164, "+52" + e164[4:]]
    if e164.startswith("+52") and not e164.startswith("+521"):
        return [e164, "+521" + e164[3:]]
    return [e164]



def _touchpoint_exists(message_sid: str) -> bool:
    if not message_sid:
        return False
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("channel", "whatsapp")
            .eq("event_type", "inbound")
            .contains("payload", {"message_sid": message_sid})
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


# Helper to check if we've already sent a ticket for a given lead and tier
def _already_sent_ticket(lead_id: str, tier: str) -> bool:
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", "ticket_sent")
            .contains("payload", {"tier": tier})
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


# Helper to check if we've already sent a media (e.g. video) for a given lead and key
def _already_sent_media(lead_id: str, key: str) -> bool:
    try:
        r = (
            sb.table("touchpoints")
            .select("id")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", "media_sent")
            .contains("payload", {"key": key})
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


def _load_recent_conversation(lead_id: str, limit: int = 16) -> list[dict[str, str]]:
    try:
        r = (
            sb.table("touchpoints")
            .select("event_type,payload,created_at")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .in_("event_type", ["inbound", "outbound_ai"])
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(reversed(r.data or []))
        out: list[dict[str, str]] = []
        for row in rows:
            et = row.get("event_type")
            p = row.get("payload") or {}
            txt = (p.get("body") or "").strip()
            if not txt:
                continue
            out.append({"role": "user" if et == "inbound" else "assistant", "content": txt})
        return out
    except Exception:
        return []


def _last_outbound(lead_id: str) -> str:
    try:
        r = (
            sb.table("touchpoints")
            .select("payload,created_at")
            .eq("lead_id", lead_id)
            .eq("channel", "whatsapp")
            .eq("event_type", "outbound_ai")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        row = (r.data or [{}])[0] or {}
        p = row.get("payload") or {}
        return (p.get("body") or "").strip()
    except Exception:
        return ""


def _is_first_contact(lead_id: str) -> bool:
    return _last_outbound(lead_id) == ""


def _google_calendar_url(facts: dict[str, str]) -> str:
    """Build a Google Calendar 'add event' URL from event facts."""
    from urllib.parse import quote_plus
    name = facts.get("event_name") or "Beyond Wealth"
    place = facts.get("event_place") or ""
    speakers = facts.get("event_speakers") or ""
    date_raw = facts.get("event_date") or ""

    # Try to parse ISO datetime for proper calendar format
    # Default: 2026-03-27T15:00 to 2026-03-30T01:30
    start_dt = "20260327T150000Z"
    end_dt = "20260330T013000Z"
    try:
        if "T" in date_raw:
            clean_dt = date_raw.replace("+00:00", "").replace("-", "").replace(":", "")
            if len(clean_dt) >= 15:
                start_dt = clean_dt[:15] + "Z"
    except Exception:
        pass

    details = f"{name}\nSpeakers: {speakers}\nLugar: {place}"
    url = (
        "https://calendar.google.com/calendar/render?"
        f"action=TEMPLATE"
        f"&text={quote_plus(name)}"
        f"&details={quote_plus(details)}"
        f"&dates={start_dt}/{end_dt}"
        f"&location={quote_plus(place)}"
    )
    return url


def _event_facts(event_id: Optional[str]) -> dict[str, str]:
    event: dict[str, Any] = {}
    if event_id:
        try:
            ev = sb.table("events").select("*").eq("event_id", event_id).limit(1).execute()
            event = (ev.data or [{}])[0] or {}
        except Exception:
            event = {}

    return {
        "event_id": event_id or "",
        "event_name": (event.get("event_name") or settings.event_name or "el evento").strip(),
        "event_date": (str(event.get("starts_at") or "") or settings.event_date or "").strip(),
        "event_place": (event.get("address") or settings.event_place or "").strip(),
        "event_speakers": (event.get("speakers") or settings.event_speakers or DEFAULT_SPEAKERS).strip(),
        "vip_price": (str(event.get("vip_price_usd") or "") or settings.vip_price or "").strip(),
    }


@router.post("/inbound")
async def whatsapp_inbound(request: Request):
    # Parse Twilio form
    try:
        form_any: Any = await request.form()
        form = dict(form_any)
    except Exception:
        form = _parse_twilio_form(await request.body())

    from_raw = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()
    message_sid = (form.get("MessageSid") or "").strip()

    wa_from = normalize_mx_whatsapp(from_raw)
    # Normalized WhatsApp sender. Twilio uses `whatsapp:+E164`, but our DB stores just `+E164`.
    wa_e164 = wa_from.replace("whatsapp:", "")

    # Extract contact info from the message (best-effort)
    msg_email = _extract_email(body)
    msg_name = _extract_name(body)
    msg_name_only = _looks_like_name_only(body)

    # Idempotency (avoid double replies)
    if message_sid and _touchpoint_exists(message_sid):
        return _twiml_empty()

    # Find lead by whatsapp variants
    lead = None
    for candidate in _mx_variants(wa_e164):
        try:
            lr = sb.table("leads").select("*").eq("whatsapp", candidate).limit(1).execute()
            lead = (lr.data or [None])[0]
            if lead:
                wa_e164 = candidate
                wa_from = f"whatsapp:{candidate}"
                break
        except Exception:
            pass

    # If not found, allow linking by email in message
    if not lead:
        email = _extract_email(body)
        if email:
            try:
                by_email = sb.table("leads").select("*").eq("email", email).limit(1).execute()
                lead = (by_email.data or [None])[0]
                if lead:
                    try:
                        sb.table("leads").update({"whatsapp": wa_e164}).eq("lead_id", lead["lead_id"]).execute()
                        lead["whatsapp"] = wa_e164
                    except Exception:
                        pass
            except Exception:
                lead = None

    # Log inbound
    try:
        sb.table("touchpoints").insert(
            {
                "lead_id": lead["lead_id"] if lead else f"wa:{wa_e164}",
                "channel": "whatsapp",
                "event_type": "inbound",
                "payload": {"from": wa_from, "body": body, "message_sid": message_sid},
            }
        ).execute()
    except Exception:
        pass

    # If no lead: auto-create one so new numbers can start the flow immediately
    if not lead:
        lead_id_new = f"wa_{uuid.uuid4().hex[:12]}"
        default_event_id = getattr(settings, "default_event_id", None) or getattr(settings, "event_id", None) or None
        # Build a minimal lead row that matches our DB schema expectations.
        # IMPORTANT: `phone` is commonly required/not-null in our leads table, so always set it.
        # NOTE: Do NOT set payment_status on insert. The DB has a check constraint (leads_payment_status_check) and may enforce a default; omitting the field is safest.
        new_lead: dict[str, Any] = {
            "lead_id": lead_id_new,
            "whatsapp": wa_e164,
            "phone": wa_e164,
            "event_id": default_event_id,
            "status": "NEW",
            "tier_interest": "NONE",
        }

        # If the user already provided contact info in the first message, include it in the insert
        # (this reduces follow-ups and helps satisfy any NOT NULL constraints).
        if msg_email:
            new_lead["email"] = msg_email
        if msg_name:
            new_lead["name"] = msg_name

        try:
            ins = sb.table("leads").insert(new_lead).execute()
            # Supabase returns inserted rows in `data` by default; prefer that as the canonical lead object.
            if getattr(ins, "data", None):
                lead = (ins.data or [new_lead])[0] or new_lead
            else:
                lead = new_lead
        except Exception as e:
            # Log the full exception so we can diagnose schema/RLS issues quickly.
            logger.exception("lead_autocreate_failed")
            # If we can't create the lead, fall back to asking for email
            return _twiml_message(
                "¡Hola! 😊 Soy Ana del equipo.\n\nNo pude crear tu registro automáticamente.\n"
                "Para registrarte rápido, compárteme tu *correo* y tu *nombre* (en un solo mensaje)."
            )

        # Best-effort: persist any info the user already provided in their first message
        try:
            updates: dict[str, Any] = {}
            if msg_email:
                updates["email"] = msg_email
            if msg_name:
                updates["name"] = msg_name
            # Keep phone in sync with WhatsApp sender
            updates["phone"] = wa_e164
            if updates:
                try:
                    sb.table("leads").update(updates).eq("lead_id", lead_id_new).execute()
                    # Keep local copy in sync
                    lead.update(updates)
                except Exception:
                    pass
        except Exception:
            pass

        # If user already provided name+email in first message, skip the greeting
        # and go straight to auto-confirm GENERAL (background handler).
        if msg_name and msg_email:
            asyncio.create_task(
                _handle_existing_lead(
                    lead=lead,
                    lead_id=lead["lead_id"],
                    body=body,
                    wa_from=wa_from,
                    wa_e164=wa_e164,
                    message_sid=message_sid,
                    msg_email=msg_email,
                    msg_name=msg_name,
                    msg_name_only=msg_name_only,
                )
            )
            return _twiml_empty()

        # Friendly first touch for new leads (need name+email to confirm)
        return _twiml_message(
            "¡Hola! 😊 Soy Ana del equipo de Beyond Wealth.\n\n"
            "Ya te aparte un lugar en *GENERAL* (gratis).\n\n"
            "Para confirmarlo: ¿me compartes tu *nombre* y tu *correo* (en un solo mensaje)?"
        )

    lead_id = lead["lead_id"]

    # Launch heavy processing in background; return empty TwiML immediately
    # so Twilio doesn't time out (~15 s limit).
    asyncio.create_task(
        _handle_existing_lead(
            lead=lead,
            lead_id=lead_id,
            body=body,
            wa_from=wa_from,
            wa_e164=wa_e164,
            message_sid=message_sid,
            msg_email=msg_email,
            msg_name=msg_name,
            msg_name_only=msg_name_only,
        )
    )
    return _twiml_empty()


# ---------------------------------------------------------------------------
# Background handler – all heavy processing for existing leads
# ---------------------------------------------------------------------------

async def _handle_existing_lead(
    lead: dict[str, Any],
    lead_id: str,
    body: str,
    wa_from: str,
    wa_e164: str,
    message_sid: str,
    msg_email: Optional[str],
    msg_name: Optional[str],
    msg_name_only: Optional[str],
) -> None:
    try:
        # --- Companion capture (only when user provides companion details in-message) ---
        # If the user sends something like:
        #   Florencia Montoya\nflorencia@example.com\n+1 (954) 756-4662
        # we create a new lead for that companion and store it as the latest companion for this lead.
        try:
            lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
            comp_email = _extract_email(body)
            comp_phone = _extract_phone_e164(body)

            # Pick a name line: first line that is not email-like and has no digits
            comp_name = None
            for ln in lines:
                if "@" in ln:
                    continue
                if re.search(r"\d", ln):
                    continue
                if len(ln) < 2 or len(ln) > 80:
                    continue
                comp_name = ln
                break

            has_companion_bundle = bool(comp_email and comp_phone and comp_name)

            if has_companion_bundle:
                companion_lead = None
                # Try to find an existing lead by email first
                try:
                    by_email = sb.table("leads").select("*").eq("email", comp_email).limit(1).execute()
                    companion_lead = (by_email.data or [None])[0]
                except Exception:
                    companion_lead = None

                # If not found by email, try by whatsapp/phone
                if not companion_lead and comp_phone:
                    for candidate in _mx_variants(comp_phone):
                        try:
                            by_wa = sb.table("leads").select("*").eq("whatsapp", candidate).limit(1).execute()
                            companion_lead = (by_wa.data or [None])[0]
                            if companion_lead:
                                break
                        except Exception:
                            pass

                # If still not found, create the companion lead
                if not companion_lead:
                    comp_lead_id_new = f"wa_{uuid.uuid4().hex[:12]}"
                    companion_row: dict[str, Any] = {
                        "lead_id": comp_lead_id_new,
                        "event_id": lead.get("event_id"),
                        "status": "NEW",
                        "tier_interest": "VIP",
                        "name": comp_name,
                        "email": comp_email,
                        # Keep schema-safe: ensure phone present
                        "phone": comp_phone,
                        "whatsapp": comp_phone,
                    }
                    try:
                        ins = sb.table("leads").insert(companion_row).execute()
                        if getattr(ins, "data", None):
                            companion_lead = (ins.data or [companion_row])[0] or companion_row
                        else:
                            companion_lead = companion_row
                    except Exception:
                        companion_lead = None

                # Store a pointer to the latest companion lead for later payment-link generation
                # Skip if companion is the same as the main lead (self-reference)
                if companion_lead and companion_lead.get("lead_id") and companion_lead.get("lead_id") != lead_id:
                    try:
                        sb.table("touchpoints").insert(
                            {
                                "lead_id": lead_id,
                                "channel": "whatsapp",
                                "event_type": "companion_created",
                                "payload": {
                                    "companion_lead_id": companion_lead.get("lead_id"),
                                    "name": companion_lead.get("name") or comp_name,
                                    "email": companion_lead.get("email") or comp_email,
                                    "phone": companion_lead.get("phone") or comp_phone,
                                },
                            }
                        ).execute()
                    except Exception:
                        pass
        except Exception:
            pass

        # Best-effort: keep lead contact info updated over time
        try:
            updates: dict[str, Any] = {}
            # Always ensure whatsapp is stored (normalized)
            if (lead.get("whatsapp") or "").strip() != wa_e164:
                updates["whatsapp"] = wa_e164

            if msg_email and not (lead.get("email") or "").strip():
                updates["email"] = msg_email

            # Name updates:
            # - If user used an explicit pattern (soy/me llamo/mi nombre es), always trust it and overwrite.
            # - If user sent a "name-only" message, only use it when we don't have a good name yet,
            #   or when the existing name looks like a mistaken capture (e.g. "Ya pagué").
            incoming_name = msg_name or msg_name_only
            if incoming_name:
                existing_name = (lead.get("name") or "").strip()
                existing_low = existing_name.lower()

                bad_names = {
                    "si me encantaría",
                    "si me encantaria",
                    "sí me encantaría",
                    "hola",
                    "ok",
                    "gracias",
                }

                looks_like_mistake = (
                    (not existing_name)
                    or (existing_low in bad_names)
                    or ("pagu" in existing_low)
                    or ("pago" in existing_low)
                    or (existing_low in {"ya pague", "ya pagué", "ya pago"})
                )

                # If it's an explicit-intent name, overwrite. Otherwise, only overwrite when existing looks wrong.
                if msg_name or looks_like_mistake:
                    updates["name"] = incoming_name

            # Store phone redundantly if your schema has it (safe if it doesn't)
            if not (lead.get("phone") or "").strip():
                updates["phone"] = wa_e164

            if updates:
                try:
                    sb.table("leads").update(updates).eq("lead_id", lead_id).execute()
                    lead.update(updates)
                except Exception:
                    pass
        except Exception:
            pass
        event_id = lead.get("event_id")
        facts = _event_facts(event_id)

        # ---------------------------------------------------------------
        # AUTO-CONFIRM GENERAL when user provides name + email
        # This is the SECOND message (after the greeting that asked for data).
        # When the lead is still NEW and the user just sent name+email,
        # confirm GENERAL, send ticket + calendar, ask about VIP, and RETURN.
        # ---------------------------------------------------------------
        just_provided_name = bool(msg_name or msg_name_only)
        just_provided_email = bool(msg_email)
        lead_is_new = str((lead.get("status") or "")).upper() in ("NEW", "")

        if just_provided_name and just_provided_email and lead_is_new:
            # Update status
            try:
                sb.table("leads").update({"status": "GENERAL_CONFIRMED", "payment_status": "FREE"}).eq("lead_id", lead_id).execute()
                lead["status"] = "GENERAL_CONFIRMED"
                lead["payment_status"] = "FREE"
            except Exception:
                pass

            if settings.public_base_url:
                ticket = generate_ticket_png(lead=lead, tier="GENERAL", event=facts)
                ticket_url = f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
                cal_url = _google_calendar_url(facts)
                ticket_msg = (
                    "✅ Listo. Ya tienes tu acceso *GENERAL* confirmado (sin costo).\n"
                    "Aqui esta tu boleto con QR 👇\n\n"
                    "📅 Agregalo a tu calendario:\n" + cal_url + "\n\n"
                    "¿Te gustaria conocer el pase *VIP* y saber todo lo que incluye?"
                ).strip()

                # Save outbound
                try:
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "outbound_ai",
                            "payload": {"to": wa_from, "body": ticket_msg, "in_reply_to": message_sid},
                        }
                    ).execute()
                except Exception:
                    pass

                # Mark ticket sent
                try:
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "ticket_sent",
                            "payload": {"tier": "GENERAL", "ticket_id": ticket["ticket_id"]},
                        }
                    ).execute()
                except Exception:
                    pass

                # Send ticket + message
                await send_whatsapp(to_e164=wa_e164, body=ticket_msg, media_urls=[ticket_url])
            else:
                fallback_msg = "✅ Listo. Tu lugar en *GENERAL* esta confirmado."
                try:
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "outbound_ai",
                            "payload": {"to": wa_from, "body": fallback_msg, "in_reply_to": message_sid},
                        }
                    ).execute()
                except Exception:
                    pass
                await send_whatsapp(to_e164=wa_e164, body=fallback_msg)

            return  # Done — ticket sent, don't continue to AI reply

        # Build conversation context
        convo = _load_recent_conversation(lead_id)
        if not convo or convo[-1].get("role") != "user" or (convo[-1].get("content") or "").strip() != body:
            convo.append({"role": "user", "content": body})

        # Generate AI reply
        ai = await generate_reply(lead=lead, event_facts=facts, conversation=convo)

        if not ai:
            # Hard fallback
            ai = (
                f"Hola {(lead.get('name') or '😊')} 👋 Soy Ana del equipo.\n"
                f"Vi tu registro a *{facts['event_name']}*.\n\n"
                "Para cuidarte tu lugar: ¿sí vas a poder asistir? (Sí/No)\n"
                "Y dime: ¿qué te llamó la atención del evento?"
            )

        clean, tokens = strip_tokens(ai)
        clean = _strip_media_placeholders(clean)
        # Remove common placeholder URL if the model outputs it (we always prefer real media/URLs)
        clean = re.sub(r"https?://(www\.)?linkdelvideo\.com/?", "", clean, flags=re.I).strip()

        # --- Quick intent heuristics (keeps the experience smooth even if the model doesn't emit tokens) ---
        low = (body or "").strip().lower()
        wants_general = any(
            k in low
            for k in [
                "general",
                "gral",
                "entrada general",
                "boleto general",
                "me quedo con general",
                "me quedo con el general",
                "solo general",
                "sin vip",
                "no vip",
                "no quiero vip",
            ]
        )
        wants_vip = any(k in low for k in ["vip", "quiero vip", "sí vip", "si vip", "pagar vip", "pago vip"])
        vip_context = (
            wants_vip
            or ("vip" in low)
            or any(
                k in low
                for k in [
                    "incluye",
                    "incluye el vip",
                    "qué incluye",
                    "que incluye",
                    "precio",
                    "cuánto cuesta",
                    "cuanto cuesta",
                    "video",
                    "tienes algún video",
                    "tienes algun video",
                    "tienes un video",
                    "me gustaría escuchar",
                    "me gustaria escuchar",
                    "quiero escuchar",
                    "saber más",
                    "saber mas",
                    "más info",
                    "mas info",
                ]
            )
        )

        asks_vip_details = any(k in low for k in [
            "que incluye", "qué incluye", "incluye el vip", "como es el vip", "cómo es el vip",
            "beneficios", "ventajas", "precio", "cuanto cuesta", "cuánto cuesta",
            "vip incluye", "detalles vip",
            "saber más del vip", "saber mas del vip", "quiero saber más del vip", "quiero saber mas del vip",
        ])

        asks_video = any(
            k in low
            for k in [
                "video",
                "vídeo",
                "mandame el video",
                "mándame el video",
                "me mandas el video",
                "me mandas vídeo",
                "no me llegó el video",
                "no me llego el video",
                "no me llegó el vídeo",
                "no me llego el vídeo",
                "no me llego",
                "no me llegó",
            ]
        )

        media_urls: list[str] = []
        clean_low = (clean or "").lower()

        # We want the VIP video to be sent the first time the user hears the VIP details.
        # Heuristic: if this outbound message contains VIP-benefit keywords (i.e., it's the VIP explainer), attach the video.
        vip_explainer_message = any(
            k in clean_low
            for k in [
                "acceso vip",
                "boleto vip",
                "experiencia vip",
                "incluye",
                "asientos",
                "primera fila",
                "acceso preferencial",
                "mastermind",
                "libro",
                "foto",
                "regalos",
            ]
        )

        # --- VIP PITCH: send copy FIRST (separate msg), then video (separate msg) ---
        # ONLY if VIP pitch hasn't been sent yet. If it was already sent,
        # skip the pitch entirely — user already saw it.
        vip_pitch_already_sent = _already_sent_media(lead_id, "vip_video")

        if (
            vip_context
            and (vip_explainer_message or asks_vip_details)
            and str((lead.get("payment_status") or "")).upper() != "PAID"
            and not vip_pitch_already_sent
        ):
            event_name_upper = (facts.get("event_name") or "BEYOND WEALTH").upper()
            vip_pitch_text = (
                f"VIP es la forma mas cercana, estrategica y transformadora de vivir *{event_name_upper}*.\n\n"
                "🔥 *Por que ser VIP:*\n"
                "- Asientos preferenciales\n"
                "- Mastermind intimo\n"
                "- Libro firmado\n"
                "- Foto con Spencer Hoffmann y algunos speakers\n"
                "- Sorpresas especiales\n\n"
                "(Por aqui te dejo un mensaje de Spencer sobre el VIP 👇)\n\n"
                "Puedes elegir:\n"
                "1️⃣ 1 VIP individual x 79 USD\n"
                "2️⃣ La opcion mas popular: 2 VIPs x 97 USD (promo especial)\n\n"
                "¿Te aparto 1 VIP o prefieres aprovechar la promo de 2?"
            ).strip()

            # MESSAGE 1: Send VIP pitch text FIRST (no media)
            try:
                sb.table("touchpoints").insert(
                    {
                        "lead_id": lead_id,
                        "channel": "whatsapp",
                        "event_type": "outbound_ai",
                        "payload": {"to": wa_from, "body": vip_pitch_text, "in_reply_to": message_sid, "type": "vip_pitch"},
                    }
                ).execute()
            except Exception:
                pass
            await send_whatsapp(to_e164=wa_e164, body=vip_pitch_text)

            # MESSAGE 2: Send VIP video separately (with short intro text)
            if settings.whatsapp_video_vip_pitch.strip():
                u = settings.whatsapp_video_vip_pitch.strip()
                if u.startswith("https://"):
                    video_intro = "🎥 Aqui tienes un video corto de Spencer explicando el VIP:"
                    try:
                        sb.table("touchpoints").insert(
                            {
                                "lead_id": lead_id,
                                "channel": "whatsapp",
                                "event_type": "media_sent",
                                "payload": {"key": "vip_video", "url": u},
                            }
                        ).execute()
                    except Exception:
                        pass
                    await send_whatsapp(to_e164=wa_e164, body=video_intro, media_urls=[u])

            # Update status to VIP_INTERESTED so affirmative replies trigger link generation
            try:
                sb.table("leads").update({"status": "VIP_INTERESTED"}).eq("lead_id", lead_id).execute()
                lead["status"] = "VIP_INTERESTED"
            except Exception:
                pass

            # Don't send the AI's response — we already sent the pitch + video
            return

        # VIP pitch video re-send on explicit request (user asks "mandame el video")
        should_send_vip_video = ("[[SEND_VIP_VIDEO]]" in tokens) or asks_video
        if (
            should_send_vip_video
            and settings.whatsapp_video_vip_pitch.strip()
            and asks_video
        ):
            u = settings.whatsapp_video_vip_pitch.strip()
            if u.startswith("https://"):
                media_urls.append(u)

        # --- VIP link heuristics (fix: define should_send_vip_link) ---
        asks_pay_link = any(
            k in low
            for k in [
                "donde pago",
                "dónde pago",
                "donde se paga",
                "link de pago",
                "liga de pago",
                "pagar vip",
                "pago vip",
                "quiero pagar",
                "quiero comprar",
                "checkout",
                "stripe",
                "pago el vip",
                "quiero pagarlo",
                "pasame el link",
                "pásame el link",
            ]
        )

        # Affirmative VIP intent — user is saying YES to VIP purchase.
        # Works especially when lead is already VIP_INTERESTED or VIP_LINK_SENT.
        lead_status_upper = str((lead.get("status") or "")).upper()
        affirmative_vip_intent = any(
            k in low
            for k in [
                "si me interesa",
                "sí me interesa",
                "me interesa",
                "si quiero",
                "sí quiero",
                "dale",
                "va",
                "le entro",
                "quiero 1",
                "quiero 2",
                "quiero el 1",
                "quiero el 2",
                "opción 1",
                "opcion 1",
                "opción 2",
                "opcion 2",
                "el 1",
                "el 2",
                "la 1",
                "la 2",
                "mándame el link",
                "mandame el link",
                "manda el link",
                "mándamelo",
                "mandamelo",
                "quiero vip",
                "sí vip",
                "si vip",
            ]
        ) and lead_status_upper in ("VIP_INTERESTED", "VIP_LINK_SENT")

        # If the model is clearly trying to send a link but used a placeholder, treat it as a link-intent.
        # This fixes the "primera vez no manda la liga" case (e.g. message contains "[LINK]").
        model_link_placeholder = (
            "[link" in clean_low
            or "link]" in clean_low
            or "[link]." in clean_low
            or "te dejo aquí el link" in clean_low
            or "te dejo aqui el link" in clean_low
            or "te dejo el link" in clean_low
            or "aquí tienes el link" in clean_low
            or "aqui tienes el link" in clean_low
            or "link para que lo pagues" in clean_low
            or "link para pagarlo" in clean_low
        )

        # Also detect when AI response talks about payment options/links (means it WANTS to send links)
        ai_wants_to_send_link = any(
            k in clean_low
            for k in [
                "opciones para completar",
                "link de pago",
                "liga de pago",
                "puedes elegir",
                "te comparto las opciones",
                "completar tu registro vip",
                "completar tu compra",
            ]
        )

        should_send_vip_link = (
            ("[[SEND_VIP_LINK]]" in tokens)
            or asks_pay_link
            or affirmative_vip_intent
            or ("vip" in low and "pago" in low)
            or (vip_context and model_link_placeholder)
            or ai_wants_to_send_link
        )

        # If the user asks for the payment link but the lead is already PAID, don't send a bogus/placeholder link.
        if should_send_vip_link and str((lead.get("payment_status") or "")).upper() == "PAID":
            if "pago" not in clean.lower() and "confirm" not in clean.lower():
                clean = (clean.rstrip() + "\n\n✅ Tu pago ya está confirmado. Si necesitas tu boleto/QR otra vez, dímelo y te lo reenvío.").strip()

        if should_send_vip_link and str((lead.get("payment_status") or "")).upper() != "PAID":
            # Avoid duplicating the link if the model already included it
            if "checkout.stripe.com" not in clean and "https://checkout.stripe.com" not in clean:
                try:
                    # Remove AI placeholders
                    clean = re.sub(r"\[[^\]]+\]\((https?://[^\)]+)\)", "", clean, flags=re.I).strip()
                    clean = re.sub(r"https?://(?:www\.)?example\.com\S*", "", clean, flags=re.I).strip()
                    clean = re.sub(r"https?://stripe-link-para-pago\S*", "", clean, flags=re.I).strip()
                    clean = re.sub(r"\[\s*link\s*\]", "", clean, flags=re.I).strip()
                    checkout_lead_id = lead_id

                    # companion payment logic (keep existing)
                    is_companion_payment = any(
                        k in low
                        for k in [
                            "acompanante", "acompañante", "para ella", "para el",
                            "para florencia", "solo para ella", "solo para el",
                            "para mi acompañante", "para mi acompanante",
                        ]
                    )
                    if is_companion_payment:
                        try:
                            tp = (
                                sb.table("touchpoints")
                                .select("payload,created_at")
                                .eq("lead_id", lead_id)
                                .eq("channel", "whatsapp")
                                .eq("event_type", "companion_created")
                                .order("created_at", desc=True)
                                .limit(1)
                                .execute()
                            )
                            row = (tp.data or [{}])[0] or {}
                            payload = row.get("payload") or {}
                            comp_id = (payload.get("companion_lead_id") or "").strip()
                            if comp_id:
                                checkout_lead_id = comp_id
                        except Exception:
                            pass

                    # Detect which option user wants
                    wants_option_2 = (
                        any(k in low for k in [
                            "dos", "opcion 2", "opción 2", "la 2", "el 2",
                            "quiero 2", "promo", "la promo", "los 2", "los dos",
                            "2 vip", "dos vip", "2 boletos", "dos boletos",
                        ])
                        or bool(re.search(r'\b2\b', low))
                    )
                    wants_option_1 = (
                        any(k in low for k in [
                            "uno", "opcion 1", "opción 1", "la 1", "el 1",
                            "quiero 1", "1 vip", "un vip", "individual",
                        ])
                        or bool(re.search(r'\b1\b', low))
                    )

                    if wants_option_2 and not wants_option_1:
                        # User explicitly wants option 2
                        url = await create_vip_checkout_link(lead_id=checkout_lead_id, event_id=event_id, option=2)
                        if url:
                            url = url.strip()
                            clean = (
                                "🔥 ¡Excelente eleccion! 2 VIPs x 97 USD (la promo mas popular).\n\n"
                                "Link de pago:\n" + url + "\n\n"
                                "En cuanto se confirme tu pago, te mando tu boleto VIP con QR 🎟️"
                            ).strip()
                        else:
                            clean = (clean.rstrip() + "\n\nAhorita no pude generar el link 😅 ¿Me pones *VIP* otra vez en 30 segundos?").strip()
                    elif wants_option_1:
                        # User explicitly wants option 1
                        url = await create_vip_checkout_link(lead_id=checkout_lead_id, event_id=event_id, option=1)
                        if url:
                            url = url.strip()
                            clean = (
                                "🔥 ¡Perfecto! 1 VIP individual x 79 USD.\n\n"
                                "Link de pago:\n" + url + "\n\n"
                                "En cuanto se confirme tu pago, te mando tu boleto VIP con QR 🎟️"
                            ).strip()
                        else:
                            clean = (clean.rstrip() + "\n\nAhorita no pude generar el link 😅 ¿Me pones *VIP* otra vez en 30 segundos?").strip()
                    else:
                        # Send BOTH options
                        url1 = await create_vip_checkout_link(lead_id=checkout_lead_id, event_id=event_id, option=1)
                        url2 = await create_vip_checkout_link(lead_id=checkout_lead_id, event_id=event_id, option=2)
                        links_text = ""
                        if url1:
                            links_text += f"1️⃣ 1 VIP individual x 79 USD:\n{url1.strip()}\n\n"
                        if url2:
                            links_text += f"2️⃣ 2 VIPs x 97 USD (promo especial):\n{url2.strip()}\n\n"
                        if links_text:
                            clean = (
                                links_text.strip() + "\n\n"
                                + clean.rstrip() + "\n\n"
                                "En cuanto se confirme tu pago, te mando tu boleto VIP con QR 🎟️"
                            ).strip()
                        else:
                            clean = (clean.rstrip() + "\n\nAhorita no pude generar el link 😅 ¿Me pones *VIP* otra vez en 30 segundos?").strip()
                except Exception:
                    url = None
                    clean = (clean.rstrip() + "\n\nAhorita no pude generar el link 😅 ¿Me pones *VIP* otra vez en 30 segundos?").strip()

            # Update status to VIP_LINK_SENT after successfully generating links
            if "checkout.stripe.com" in clean:
                try:
                    sb.table("leads").update({"status": "VIP_LINK_SENT"}).eq("lead_id", lead_id).execute()
                    lead["status"] = "VIP_LINK_SENT"
                except Exception:
                    pass

        # If model asked to send General ticket
        if "[[SEND_GENERAL_TICKET]]" in tokens:
            if not settings.public_base_url:
                clean = (clean + "\n\n(Nota: falta PUBLIC_BASE_URL para mandar tu QR automático.)").strip()
            else:
                ticket = generate_ticket_png(lead=lead, tier="GENERAL", event=facts)
                media_urls.append(
                    f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
                )

        # Refresh lead so we see payment_status updates made by Stripe webhook
        try:
            lead_fresh_res = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
            lead_fresh = (lead_fresh_res.data or [None])[0] or lead
            lead = lead_fresh
        except Exception:
            pass

        # If the user explicitly asks again for the QR/ticket after payment, re-send it (on demand).
        asks_qr = any(
            k in low
            for k in [
                "qr",
                "código qr",
                "codigo qr",
                "mi boleto",
                "mandas mi boleto",
                "mándas mi boleto",
                "me mandas mi boleto",
                "me mandas el boleto",
                "boleto vip",
                "imagen",
                "ticket",
            ]
        )

        # If Stripe webhook already marked this lead as PAID, automatically send VIP ticket ONCE (no user trigger needed)
        vip_ticket_auto_sent = False
        if str((lead.get("payment_status") or "")).upper() == "PAID":
            if settings.public_base_url and not _already_sent_ticket(lead_id, "VIP"):
                ticket = generate_ticket_png(lead=lead, tier="VIP", event=facts)
                media_urls.append(
                    f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
                )
                cal_url = _google_calendar_url(facts)
                clean = (
                    "✅ Pago confirmado. Aqui esta tu boleto VIP con QR 👇\n\n"
                    "📅 Agregalo a tu calendario:\n" + cal_url
                ).strip()

                # Mark that we already sent the VIP ticket
                try:
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "ticket_sent",
                            "payload": {"tier": "VIP", "ticket_id": ticket["ticket_id"]},
                        }
                    ).execute()
                except Exception:
                    pass
                vip_ticket_auto_sent = True

        # If the user explicitly asks again for the QR/ticket after payment, re-send it (on demand).
        # Skip if we just auto-sent above to avoid duplicates.
        if (
            not vip_ticket_auto_sent
            and str((lead.get("payment_status") or "")).upper() == "PAID"
            and asks_qr
            and settings.public_base_url
        ):
            ticket = generate_ticket_png(lead=lead, tier="VIP", event=facts)
            media_urls.append(
                f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
            )
            if "qr" not in clean.lower() and "boleto" not in clean.lower():
                cal_url = _google_calendar_url(facts)
                clean = (clean.rstrip() + "\n\n✅ Aqui tienes tu boleto VIP con QR 👇\n\n📅 Agregalo a tu calendario:\n" + cal_url).strip()

        # Free GENERAL flow: if the user chooses General (no payment), confirm + send ticket right away.
        # Guard: don't re-send if they already have a GENERAL ticket (e.g. from auto-confirm).
        if wants_general and not wants_vip and not _already_sent_ticket(lead_id, "GENERAL") and str((lead.get("payment_status") or "")).upper() != "PAID":
            try:
                sb.table("leads").update({"status": "GENERAL_CONFIRMED", "payment_status": "FREE"}).eq("lead_id", lead_id).execute()
                lead["status"] = "GENERAL_CONFIRMED"
                lead["payment_status"] = "FREE"
            except Exception:
                pass

            if settings.public_base_url:
                ticket = generate_ticket_png(lead=lead, tier="GENERAL", event=facts)
                media_urls.append(
                    f"{settings.public_base_url.rstrip('/')}/v1/tickets/{ticket['ticket_id']}.png?t={ticket['token']}"
                )
                cal_url = _google_calendar_url(facts)
                clean = (
                    "✅ Listo. Ya tienes tu acceso *GENERAL* confirmado (sin costo).\n"
                    "Aqui esta tu boleto con QR 👇\n\n"
                    "📅 Agregalo a tu calendario:\n" + cal_url + "\n\n"
                    "¿Te gustaria conocer el pase *VIP* y saber todo lo que incluye?"
                ).strip()

                # Mark ticket sent
                try:
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "ticket_sent",
                            "payload": {"tier": "GENERAL", "ticket_id": ticket["ticket_id"]},
                        }
                    ).execute()
                except Exception:
                    pass
            else:
                clean = (
                    "✅ Listo. Ya tienes tu acceso *GENERAL* confirmado (sin costo).\n\n"
                    "(Nota: falta PUBLIC_BASE_URL para mandar tu QR automatico.)"
                ).strip()

        # Save outbound
        try:
            sb.table("touchpoints").insert(
                {
                    "lead_id": lead_id,
                    "channel": "whatsapp",
                    "event_type": "outbound_ai",
                    "payload": {"to": wa_from, "body": clean, "in_reply_to": message_sid, "tokens": list(tokens)},
                }
            ).execute()
        except Exception:
            pass

        # De-duplicate media URLs (Twilio can be picky)
        if media_urls:
            media_urls = list(dict.fromkeys([u for u in media_urls if u]))

        # Send reply via Twilio REST API (background; TwiML already returned empty).
        await send_whatsapp(to_e164=wa_e164, body=clean, media_urls=media_urls or None)

        # --- Post-reply: send testimonial video + closing message (once per lead) ---
        lead_status = str((lead.get("status") or "")).upper()
        lead_paid = str((lead.get("payment_status") or "")).upper()
        ticket_just_sent = (
            (wants_general and lead_status in ("GENERAL_CONFIRMED",))
            or (lead_paid == "PAID" and "boleto" in clean.lower())
        )
        # Also send testimonials on first message after payment (even if ticket was sent by Stripe webhook)
        should_send_testimonials = (
            ticket_just_sent
            or (lead_paid == "PAID" and not _already_sent_media(lead_id, "testimonios"))
        )
        if should_send_testimonials and settings.whatsapp_video_testimonios.strip():
            if not _already_sent_media(lead_id, "testimonios"):
                testimonial_url = settings.whatsapp_video_testimonios.strip()
                try:
                    await send_whatsapp(
                        to_e164=wa_e164,
                        body="🎬 Mira lo que dicen quienes ya vivieron Beyond Wealth 👇",
                        media_urls=[testimonial_url],
                    )
                    sb.table("touchpoints").insert(
                        {
                            "lead_id": lead_id,
                            "channel": "whatsapp",
                            "event_type": "media_sent",
                            "payload": {"key": "testimonios", "url": testimonial_url},
                        }
                    ).execute()
                except Exception:
                    pass

                # Closing message from Ana
                try:
                    cal_url = _google_calendar_url(facts)
                    closing = (
                        "Soy Ana y me da muchisimo gusto poderte servir 😊\n\n"
                        "Estoy muy emocionada de que vayas a ser parte de *Beyond Wealth*, "
                        "un evento que puede cambiar tu vida.\n\n"
                        "Cualquier pregunta que tengas, aqui estoy para servirte.\n\n"
                        "📅 Recuerda agregar el evento a tu calendario:\n" + cal_url
                    ).strip()
                    await send_whatsapp(to_e164=wa_e164, body=closing)
                except Exception:
                    pass

    except Exception:
        logger.exception("bg_reply_failed lead=%s", lead_id)
        try:
            await send_whatsapp(to_e164=wa_e164, body="Tuve un problema técnico 😅 ¿Me escribes de nuevo?")
        except Exception:
            logger.exception("bg_fallback_also_failed lead=%s", lead_id)
