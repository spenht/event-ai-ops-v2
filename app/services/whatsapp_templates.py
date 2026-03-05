"""WhatsApp Template & Broadcast sending via Twilio Content API.

This module handles:
- Defining all WhatsApp marketing templates (reminders, gifts, urgency, post-event)
- Creating templates via Twilio Content API
- Submitting templates for WhatsApp approval
- Checking approval status
- Sending template-based messages (ContentSid) to individual recipients

Auth: Basic auth with TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN (same pattern as twilio_whatsapp.py).
Threading: sync httpx.Client inside anyio.to_thread.run_sync to avoid async/event-loop conflicts.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import anyio
import httpx

from ..settings import settings

logger = logging.getLogger("whatsapp_templates")

# ---------------------------------------------------------------------------
# Auth helper (mirrors twilio_whatsapp.py)
# ---------------------------------------------------------------------------

def _basic_auth_header() -> str:
    token = base64.b64encode(
        f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode("utf-8")
    ).decode("utf-8")
    return f"Basic {token}"


# ---------------------------------------------------------------------------
# Template definitions
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, dict[str, Any]] = {
    # ---- Recordatorios (Reminders) ----
    "reminder_7d": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f44b \u00a1Faltan solo 7 d\u00edas para *Beyond Wealth Miami*! "
            "\U0001f5d3\ufe0f 27 - 29 de Marzo "
            "\U0001f4cd EB Hotel Miami "
            "\u00bfYa agendaste la fecha? Este evento va a marcar un antes y un despu\u00e9s. "
            "\u00a1Te esperamos! \U0001f525"
        ),
    },
    "reminder_3d": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f525 \u00a1Faltan 3 d\u00edas para *Beyond Wealth Miami*! "
            "Tips para aprovecharlo al m\u00e1ximo: "
            "\u2705 Llega temprano para los mejores lugares "
            "\u2705 Trae libreta y pluma "
            "\u2705 Ven con mentalidad abierta "
            "\U0001f4cd EB Hotel Miami "
            "\u23f0 Registro desde las 8:00 AM "
            "\u00a1Nos vemos! \U0001f4aa"
        ),
    },
    "reminder_1d": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f6a8 \u00a1MA\u00d1ANA es *Beyond Wealth Miami*! "
            "\U0001f4cd EB Hotel Miami - Sal\u00f3n EB Grand Plus "
            "\U0001f4cd 4299 NW 36 St, Miami Springs FL "
            "\u23f0 Registro desde las 8:00 AM "
            "\U0001f3ab Ten tu boleto QR listo "
            "\U0001f4dd Trae libreta y pluma "
            "\U0001f50b Carga tu cel al 100% "
            "\u00a1Prep\u00e1rate para una experiencia transformadora! \U0001f525"
        ),
    },
    "reminder_day_of": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f389 \u00a1HOY ES EL D\u00cdA! *Beyond Wealth Miami* te espera. "
            "\U0001f4cd EB Hotel Miami - Sal\u00f3n EB Grand Plus "
            "\U0001f4cd 4299 NW 36 St, Miami Springs FL "
            "\u23f0 Registro desde las 8:00 AM "
            "\U0001f3ab Ten tu boleto QR listo "
            "\u00a1Te veo ah\u00ed! \U0001f4aa\U0001f525"
        ),
    },
    # ---- Regalos / Sorpresas ----
    "gift_course": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f381 \u00a1REGALO ESPECIAL para ti! "
            "Por ser parte de *Beyond Wealth Miami*, te vamos a regalar un curso completo de "
            "*Mentalidad de \u00c9xito* (valor: $997 USD) completamente GRATIS. "
            "Es el mismo programa que ha transformado la mentalidad financiera de cientos de personas. "
            "Lo recibir\u00e1s durante el evento. \u00a1Es tu momento! \U0001f680"
        ),
    },
    "gift_raffle": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f3b0 \u00a1SORPRESA! Tenemos RIFAS incre\u00edbles en *Beyond Wealth Miami*. "
            "Premios: "
            "\U0001f34e iPad "
            "\U0001f4f1 iPhone "
            "\U0001f4bb MacBook "
            "Tu boleto de entrada ES tu boleto para las rifas. "
            "Solo necesitas ESTAR PRESENTE para participar. "
            "\u00a1Una raz\u00f3n m\u00e1s para no faltar! \U0001f525"
        ),
    },
    # ---- Urgencia VIP ----
    "vip_urgency": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \u26a1 Quedan MUY POCOS lugares *VIP* para Beyond Wealth Miami. "
            "El VIP incluye: "
            "\U0001f947 Primera fila "
            "\U0001f4d6 Libro firmado "
            "\U0001f4f8 Foto con Spencer "
            "\U0001f9e0 Mastermind \u00edntimo "
            "\U0001f381 Regalos exclusivos "
            "1 VIP: $79 USD | 2 VIPs: $97 USD "
            "Responde *VIP* y te mando el link \U0001f525"
        ),
    },
    "vip_last_chance": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f6a8 \u00daLTIMA OPORTUNIDAD de upgrade a *VIP* en Beyond Wealth Miami. "
            "El evento es muy pronto y los lugares VIP est\u00e1n casi agotados. "
            "No te quedes sin la experiencia completa: primera fila, mastermind con Spencer, "
            "libro firmado y foto. "
            "Responde *VIP* si quieres asegurar tu lugar \U0001f525"
        ),
    },
    # ---- Post-evento ----
    "post_event_thanks": {
        "category": "MARKETING",
        "language": "es",
        "body": (
            "{{1}} \U0001f64f \u00a1Gracias por ser parte de *Beyond Wealth Miami*! "
            "Fue un honor tenerte. Espero que hayas salido con ideas claras y energ\u00eda "
            "para transformar tu vida financiera. "
            "Pronto te compartir\u00e9 algo especial. \u00a1Sigue conectado/a! \U0001f4aa"
        ),
    },
}


# ---------------------------------------------------------------------------
# Twilio Content API helpers (sync, run in thread)
# ---------------------------------------------------------------------------

CONTENT_API_BASE = "https://content.twilio.com/v1/Content"


def _create_template_sync(name: str, body: str, language: str) -> dict[str, Any]:
    """Create a single template via Twilio Content API (synchronous).

    POST https://content.twilio.com/v1/Content
    Returns the full JSON response including 'sid'.
    """
    payload = {
        "friendly_name": name,
        "language": language,
        "types": {
            "twilio/text": {
                "body": body,
            }
        },
        "variables": {
            "1": "nombre",      # placeholder sample value
        },
    }

    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        r = client.post(CONTENT_API_BASE, json=payload, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "create_template_failed name=%s status=%s body=%s",
                name, r.status_code, r.text[:1200],
            )
            r.raise_for_status()
        result = r.json()
        logger.info("template_created name=%s sid=%s", name, result.get("sid"))
        return result


def _submit_approval_sync(content_sid: str, name: str, category: str) -> dict[str, Any]:
    """Submit a content template for WhatsApp approval (synchronous).

    POST https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests
    """
    url = f"{CONTENT_API_BASE}/{content_sid}/ApprovalRequests"
    payload = {
        "name": name,
        "category": category,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "submit_approval_failed sid=%s status=%s body=%s",
                content_sid, r.status_code, r.text[:1200],
            )
            r.raise_for_status()
        result = r.json()
        logger.info("approval_submitted sid=%s name=%s", content_sid, name)
        return result


def _get_approval_status_sync(content_sid: str) -> dict[str, Any]:
    """Fetch approval status for a content template (synchronous).

    GET https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests
    """
    url = f"{CONTENT_API_BASE}/{content_sid}/ApprovalRequests"
    headers = {"Authorization": _basic_auth_header()}

    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "get_approval_failed sid=%s status=%s body=%s",
                content_sid, r.status_code, r.text[:1200],
            )
            r.raise_for_status()
        return r.json()


def _send_template_sync(
    to_e164: str,
    content_sid: str,
    variables: dict[str, str],
) -> str:
    """Send a WhatsApp template message using ContentSid (synchronous).

    Uses the standard Twilio Messages API but with ContentSid + ContentVariables
    instead of Body.

    Returns the Twilio Message SID.
    """
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages.json"
    )
    to_value = f"whatsapp:{to_e164}" if not to_e164.startswith("whatsapp:") else to_e164

    data = {
        "From": settings.twilio_whatsapp_from,
        "To": to_value,
        "ContentSid": content_sid,
        "ContentVariables": json.dumps(variables),
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, data=data, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "template_send_failed to=%s sid=%s status=%s body=%s",
                to_value, content_sid, r.status_code, r.text[:1200],
            )
            r.raise_for_status()
        payload = r.json()
        msg_sid = (payload.get("sid") or "").strip()
        logger.info("template_sent msg_sid=%s to=%s content_sid=%s", msg_sid, to_value, content_sid)
        return msg_sid


# ---------------------------------------------------------------------------
# Async public API (wraps sync calls in threads)
# ---------------------------------------------------------------------------

async def create_all_templates() -> list[dict[str, str]]:
    """Create ALL defined templates via Twilio Content API.

    Returns list of dicts with keys: name, content_sid, body, language, category.
    """
    results: list[dict[str, str]] = []

    for name, tmpl in TEMPLATES.items():
        body = tmpl["body"]
        language = tmpl["language"]
        category = tmpl["category"]

        try:
            resp = await anyio.to_thread.run_sync(
                lambda n=name, b=body, l=language: _create_template_sync(n, b, l)
            )
            content_sid = resp.get("sid", "")
            results.append({
                "name": name,
                "content_sid": content_sid,
                "body": body,
                "language": language,
                "category": category,
            })
        except Exception as e:
            logger.error("create_template_error name=%s err=%s", name, str(e)[:300])
            results.append({
                "name": name,
                "content_sid": "",
                "body": body,
                "language": language,
                "category": category,
                "error": str(e)[:300],
            })

    return results


async def submit_for_approval(content_sid: str, name: str, category: str) -> dict[str, Any]:
    """Submit a content template for WhatsApp approval."""
    return await anyio.to_thread.run_sync(
        lambda: _submit_approval_sync(content_sid, name, category)
    )


async def get_template_status(content_sid: str) -> dict[str, Any]:
    """Get the approval status of a content template."""
    return await anyio.to_thread.run_sync(
        lambda: _get_approval_status_sync(content_sid)
    )


async def send_whatsapp_template(
    to_e164: str,
    content_sid: str,
    variables: dict[str, str],
) -> str:
    """Send a WhatsApp template message to a single recipient.

    Args:
        to_e164: Recipient phone in E.164 format (e.g. +1234567890)
        content_sid: Twilio Content SID for the approved template
        variables: Template variable substitutions, e.g. {"1": "Juan"}

    Returns:
        Twilio Message SID.
    """
    return await anyio.to_thread.run_sync(
        lambda: _send_template_sync(to_e164, content_sid, variables)
    )
