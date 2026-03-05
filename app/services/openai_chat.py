from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from ..settings import settings

logger = logging.getLogger("openai_chat")

TOKENS = {
    "SEND_VIP_LINK": "[[SEND_VIP_LINK]]",
    "SEND_VIP_VIDEO": "[[SEND_VIP_VIDEO]]",
    "SEND_GENERAL_TICKET": "[[SEND_GENERAL_TICKET]]",
}


def _read_prompt() -> str:
    if settings.whatsapp_system_prompt_env.strip():
        return settings.whatsapp_system_prompt_env.strip()

    try:
        p = Path(settings.whatsapp_system_prompt_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    # Sensible default if prompt not found
    return (
        "Eres Ana del equipo del evento. Estás chateando por WhatsApp con alguien que ya se registró.\n"
        "Objetivo: confirmar asistencia (General) -> conectar con su para qué -> ofrecer VIP (upgrade opcional).\n"
        "Reglas:\n"
        "- Suena humana y natural. Mensajes cortos.\n"
        "- NO repitas '¿quieres VIP?' en cada mensaje.\n"
        "- Si el usuario confirma asistencia General, pide nombre si falta y luego devuelve [[SEND_GENERAL_TICKET]].\n"
        "- Si el usuario quiere VIP, devuelve [[SEND_VIP_LINK]] y (si hay video) [[SEND_VIP_VIDEO]].\n"
        "- Nunca digas que VIP es obligatorio.\n"
    )


def strip_tokens(text: str) -> tuple[str, set[str]]:
    found: set[str] = set()
    out = (text or "").strip()
    for t in TOKENS.values():
        if t in out:
            found.add(t)
            out = out.replace(t, "")
    out = "\n".join([ln.rstrip() for ln in out.splitlines() if ln.strip()])
    return out.strip(), found


async def generate_reply(
    *,
    lead: dict[str, Any],
    event_facts: dict[str, Any],
    conversation: list[dict[str, str]],
) -> Optional[str]:
    if not settings.openai_api_key:
        logger.error("Missing OPENAI_API_KEY")
        return None

    system_prompt = _read_prompt()

    facts_block = {
        "lead": {
            "lead_id": lead.get("lead_id"),
            "name": lead.get("name"),
            "email": lead.get("email"),
            "whatsapp": lead.get("whatsapp"),
            "status": lead.get("status"),
            "payment_status": lead.get("payment_status"),
        },
        "event": event_facts,
        "capabilities": {
            "vip_video_available": bool(settings.whatsapp_video_vip_pitch.strip()),
        },
        "tokens": TOKENS,
    }

    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "DATOS (fuente de verdad): " + json.dumps(facts_block, ensure_ascii=False)},
            *conversation,
        ],
        "max_output_tokens": 420,
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                logger.error("openai_http_error status=%s body=%s", resp.status_code, resp.text[:1200])
                return None
            data = resp.json()

        if isinstance(data.get("output_text"), str) and data["output_text"].strip():
            return data["output_text"].strip()

        parts: list[str] = []
        for item in data.get("output", []) or []:
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text" and c.get("text"):
                    parts.append(c["text"])
        txt = "\n".join(parts).strip()
        return txt or None

    except Exception as e:
        logger.exception("openai_exception %s", str(e)[:300])
        return None
