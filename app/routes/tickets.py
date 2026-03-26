from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services.tickets import lookup_ticket
from ..settings import settings

logger = logging.getLogger("tickets")

router = APIRouter(prefix="/v1/tickets", tags=["tickets"])


@router.get("/{ticket_id}.png")
def get_ticket_png(ticket_id: str, t: str = Query(default="", description="access token")):
    rec = lookup_ticket(ticket_id)
    if not rec:
        raise HTTPException(status_code=404, detail="ticket not found")

    if not t or t != rec.get("token"):
        raise HTTPException(status_code=403, detail="forbidden")

    fp = Path(rec.get("file") or "")
    if not fp.exists() or not fp.is_file():
        # File was lost (deploy/restart) — regenerate from DB data
        try:
            from ..services.tickets import regenerate_ticket_png
            new_fp = regenerate_ticket_png(ticket_id)
            if new_fp:
                fp = Path(new_fp)
        except Exception:
            pass
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="file missing")

    # Twilio reads content-type to classify media.
    return FileResponse(
        str(fp),
        media_type="image/png",
        filename=fp.name,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# AI Design Generator
# ---------------------------------------------------------------------------

class DesignPromptRequest(BaseModel):
    campaign_id: str
    prompt: str


@router.post("/design-prompt")
async def generate_ticket_design(body: DesignPromptRequest):
    """Generate a ticket color/style design from a natural language prompt using AI."""
    api_key = settings.openai_api_key
    if not api_key:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    system = (
        "You are a graphic design AI. Given a description of a ticket design style, "
        "return ONLY a JSON object with these exact keys (all values are CSS color strings or short text):\n"
        "{\n"
        '  "bg_gradient_from": "#hex",\n'
        '  "bg_gradient_to": "#hex",\n'
        '  "bg_gradient_direction": "to bottom right",\n'
        '  "text_primary": "#hex",\n'
        '  "text_secondary": "#hex",\n'
        '  "text_brand": "#hex",\n'
        '  "accent_color": "#hex",\n'
        '  "font_style": "modern|classic|elegant|bold|minimalist",\n'
        '  "border_style": "none|solid|rounded|double",\n'
        '  "border_color": "#hex",\n'
        '  "qr_bg": "#hex",\n'
        '  "overall_vibe": "short 2-3 word description"\n'
        "}\n"
        "Return ONLY valid JSON, no markdown, no explanation."
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": body.prompt},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
            )
        if resp.status_code >= 400:
            logger.error("design_prompt_openai_error status=%s body=%s", resp.status_code, resp.text[:500])
            raise HTTPException(status_code=502, detail="AI service error")

        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        content = content.strip()

        design = json.loads(content)
        return {"ok": True, "design": design}

    except json.JSONDecodeError:
        logger.error("design_prompt_json_parse_failed content=%s", content[:500])
        raise HTTPException(status_code=502, detail="AI returned invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("design_prompt_failed")
        raise HTTPException(status_code=500, detail=str(e)[:200])
