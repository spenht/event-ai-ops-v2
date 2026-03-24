"""
SMS Blast — send bulk invitation SMS to leads.
POST /v1/sms/blast with campaign_id and limit.
"""
import asyncio
import logging
import time
import uuid
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from ..settings import settings

logger = logging.getLogger("sms_blast")

router = APIRouter(prefix="/v1/sms", tags=["sms"])

TOLL_FREE_NUMBER = "+18885564279"
TWILIO_SID = "ACcfbfaa84e1a092be65596efbab6af33a"
TWILIO_TOKEN = "b321f1dfe70dc7463d651008acbca9dc"

# Track blast jobs
_BLAST_JOBS: dict = {}


class BlastRequest(BaseModel):
    campaign_id: str
    limit: int = 100
    message: str = ""  # Custom message, or use default


@router.post("/blast")
async def sms_blast(request: Request, body: BlastRequest):
    """Launch SMS blast to leads who haven't been SMS'd yet."""
    from supabase import create_client
    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)

    campaign_id = body.campaign_id
    limit = min(body.limit, 1000)  # Max 1000 per blast

    # Get campaign info
    cr = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
    campaign = (cr.data or [{}])[0]
    event_name = campaign.get("event_name", "Beyond Wealth Miami")

    # Get leads with phone numbers who haven't received SMS blast yet
    # Check touchpoints to avoid double-sending
    already_sent = set()
    tp = sb.table("touchpoints").select("lead_id").eq("campaign_id", campaign_id).eq("event_type", "sms_blast_sent").execute()
    for t in (tp.data or []):
        already_sent.add(t["lead_id"])

    # Get leads with valid US phone numbers
    leads_r = sb.table("leads").select("lead_id,name,phone,whatsapp").eq("campaign_id", campaign_id).limit(limit * 2).execute()
    leads = []
    for l in (leads_r.data or []):
        if l["lead_id"] in already_sent:
            continue
        phone = (l.get("phone") or l.get("whatsapp") or "").replace("whatsapp:", "").strip()
        if phone and phone.startswith("+"):
            leads.append({"lead_id": l["lead_id"], "name": l.get("name", ""), "phone": phone})
        if len(leads) >= limit:
            break

    if not leads:
        return {"ok": False, "message": "No leads available to SMS"}

    # Create blast job
    job_id = uuid.uuid4().hex[:12]
    _BLAST_JOBS[job_id] = {"status": "processing", "total": len(leads), "sent": 0, "failed": 0, "started": time.time()}

    # Default message
    msg_template = body.message or (
        "Hola {name}! Soy Spencer Hoffmann y quiero invitarte personalmente a Beyond Wealth Miami, "
        "un evento de 3 dias 100% GRATIS este 27-29 de marzo en el EB Hotel Miami. "
        "Es para romper creencias limitantes y construir riqueza consciente. "
        "Responde SI y te mando tu boleto gratis con tu nombre!"
    )

    # Fire and forget
    asyncio.ensure_future(_run_blast(job_id, leads, msg_template, campaign_id))

    return {"ok": True, "job_id": job_id, "total_leads": len(leads), "message": "SMS blast started"}


@router.get("/blast/{job_id}")
async def blast_status(job_id: str):
    """Check blast job status."""
    job = _BLAST_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def _run_blast(job_id: str, leads: list, msg_template: str, campaign_id: str):
    """Send SMS to all leads with rate limiting."""
    import httpx
    from supabase import create_client
    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)

    sent = 0
    failed = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for i, lead in enumerate(leads):
            try:
                name = lead.get("name", "").split()[0] if lead.get("name") else ""
                msg = msg_template.replace("{name}", name or "Hola")

                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                    auth=(TWILIO_SID, TWILIO_TOKEN),
                    data={
                        "From": TOLL_FREE_NUMBER,
                        "To": lead["phone"],
                        "Body": msg,
                    },
                )
                if resp.status_code == 201:
                    sent += 1
                    # Log touchpoint to avoid re-sending
                    sb.table("touchpoints").insert({
                        "lead_id": lead["lead_id"],
                        "campaign_id": campaign_id,
                        "channel": "sms",
                        "event_type": "sms_blast_sent",
                        "payload": {"message": msg[:100], "blast_job_id": job_id},
                    }).execute()
                else:
                    failed += 1
                    logger.warning("sms_blast_fail lead=%s status=%s body=%s", lead["lead_id"], resp.status_code, resp.text[:100])
            except Exception as e:
                failed += 1
                logger.warning("sms_blast_error lead=%s err=%s", lead["lead_id"], str(e)[:100])

            _BLAST_JOBS[job_id] = {**_BLAST_JOBS[job_id], "sent": sent, "failed": failed}

            # Rate limit: 10 SMS per second max for toll-free
            if (i + 1) % 10 == 0:
                await asyncio.sleep(1.5)

    _BLAST_JOBS[job_id] = {**_BLAST_JOBS[job_id], "status": "done", "sent": sent, "failed": failed, "finished": time.time()}
    logger.info("sms_blast_done job=%s sent=%s failed=%s", job_id, sent, failed)
