"""Landing page builder endpoints."""
import logging
import uuid
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel

from ..settings import settings

logger = logging.getLogger("landing_pages")
router = APIRouter(prefix="/v1", tags=["landing-pages"])

def _sb():
    from supabase import create_client
    return create_client(settings.supabase_url, settings.supabase_service_role_key)

def _validate_auth(request: Request, campaign_id: str):
    """Validate auth — same pattern as traffic_sources.py."""
    token = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    spartans_key = (request.headers.get("x-spartans-key") or "").strip()
    cron_token = (request.headers.get("x-cron-token") or "").strip()

    if settings.cron_token and cron_token == settings.cron_token:
        return
    if spartans_key and campaign_id:
        try:
            r = _sb().table("campaigns").select("spartans_key").eq("id", campaign_id).limit(1).execute()
            camp = (r.data or [None])[0]
            if camp and camp.get("spartans_key") == spartans_key:
                return
        except Exception:
            pass
    if spartans_key and settings.spartans_key and spartans_key == settings.spartans_key:
        return
    if not settings.cron_token:
        return  # dev mode
    raise HTTPException(status_code=403, detail="invalid auth token")


# ─── Templates ──────────────────────────────────────────────────────────────

TEMPLATES = {
    "evento_presencial": {
        "name": "Evento Presencial",
        "description": "Landing para eventos de conversion en vivo",
        "sections": [
            {"type": "hero", "order": 1, "visible": True, "content": {"headline": "🔥 El Evento que Transformará tu Vida", "subheadline": "Descubre las estrategias que los más exitosos usan para multiplicar sus ingresos", "cta_text": "RESERVA TU LUGAR GRATIS →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #1a0533 0%, #0d1b2a 100%)", "text_color": "#ffffff", "cta_color": "#37ca37"}},
            {"type": "countdown", "order": 2, "visible": True, "content": {"headline": "EL EVENTO COMIENZA EN:", "target_date": ""}, "style": {"background": "#0a0a0a", "text_color": "#ffffff"}},
            {"type": "benefits", "order": 3, "visible": True, "content": {"headline": "¿Por qué asistir?", "items": [{"icon": "🎯", "title": "Estrategias Probadas", "description": "Aprende las mismas estrategias que han generado millones"}, {"icon": "🤝", "title": "Networking de Alto Nivel", "description": "Conecta con emprendedores que piensan en grande"}, {"icon": "🚀", "title": "Plan de Acción", "description": "Sal con un plan claro para escalar tu negocio"}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"type": "speakers", "order": 4, "visible": True, "content": {"headline": "Speakers Confirmados", "speakers": [{"name": "Speaker 1", "title": "CEO & Fundador", "image_url": "", "bio": ""}]}, "style": {"background": "#0d1117", "text_color": "#ffffff"}},
            {"type": "testimonials", "order": 5, "visible": True, "content": {"headline": "Lo que dicen nuestros asistentes", "items": [{"name": "María G.", "text": "Fue la mejor inversión de mi vida", "image_url": ""}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"type": "form", "order": 6, "visible": True, "content": {"headline": "🎟️ Reserva tu Lugar", "subheadline": "Completa el formulario y asegura tu entrada", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO MI LUGAR →", "success_message": "¡Listo! Revisa tu WhatsApp 📱"}, "style": {"background": "linear-gradient(135deg, #1a0533 0%, #0d1b2a 100%)", "text_color": "#ffffff", "cta_color": "#37ca37"}},
            {"type": "faq", "order": 7, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Cuánto cuesta?", "answer": "La entrada general es GRATIS. También tenemos opciones VIP con beneficios exclusivos."}, {"question": "¿Dónde es el evento?", "answer": "Te enviaremos toda la información por WhatsApp al registrarte."}]}, "style": {"background": "#0a0a0a", "text_color": "#ffffff"}}
        ],
        "theme": {"primary_color": "#37ca37", "secondary_color": "#8F73E6", "background_color": "#0d1117", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "webinar": {
        "name": "Webinar / Masterclass",
        "description": "Landing para webinars y clases en vivo online",
        "sections": [
            {"type": "hero", "order": 1, "visible": True, "content": {"headline": "Masterclass GRATIS", "subheadline": "Descubre cómo construir un negocio digital desde cero en 2026", "cta_text": "RESERVAR MI LUGAR →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", "text_color": "#ffffff", "cta_color": "#3b82f6"}},
            {"type": "benefits", "order": 2, "visible": True, "content": {"headline": "En esta masterclass aprenderás:", "items": [{"icon": "✅", "title": "Cómo elegir tu nicho perfecto", "description": ""}, {"icon": "✅", "title": "El sistema de ventas que funciona 24/7", "description": ""}, {"icon": "✅", "title": "Cómo conseguir tus primeros 100 clientes", "description": ""}]}, "style": {"background": "#1e293b", "text_color": "#ffffff"}},
            {"type": "speakers", "order": 3, "visible": True, "content": {"headline": "Tu instructor", "speakers": [{"name": "Tu Nombre", "title": "Experto en Negocios Digitales", "image_url": "", "bio": ""}]}, "style": {"background": "#0f172a", "text_color": "#ffffff"}},
            {"type": "form", "order": 4, "visible": True, "content": {"headline": "Regístrate GRATIS", "subheadline": "Cupo limitado — reserva tu lugar ahora", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO ASISTIR →", "success_message": "¡Registrado! Te enviaremos el link por WhatsApp 📱"}, "style": {"background": "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", "text_color": "#ffffff", "cta_color": "#3b82f6"}},
            {"type": "countdown", "order": 5, "visible": True, "content": {"headline": "EMPIEZA EN:", "target_date": ""}, "style": {"background": "#0f172a", "text_color": "#ffffff"}}
        ],
        "theme": {"primary_color": "#3b82f6", "secondary_color": "#8b5cf6", "background_color": "#0f172a", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "lead_magnet": {
        "name": "Lead Magnet (Ebook/Guía)",
        "description": "Landing para descargar un recurso gratuito",
        "sections": [
            {"type": "hero", "order": 1, "visible": True, "content": {"headline": "Descarga GRATIS", "subheadline": "La guía definitiva para emprendedores digitales en 2026", "cta_text": "DESCARGAR AHORA →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #fafafa 0%, #f0f0f0 100%)", "text_color": "#111111", "cta_color": "#f59e0b"}},
            {"type": "benefits", "order": 2, "visible": True, "content": {"headline": "Lo que encontrarás dentro:", "items": [{"icon": "📖", "title": "Capítulo 1: Fundamentos", "description": "Todo lo que necesitas saber para empezar"}, {"icon": "💡", "title": "Capítulo 2: Estrategias", "description": "Las tácticas que realmente funcionan"}, {"icon": "🎯", "title": "Capítulo 3: Plan de Acción", "description": "Tu roadmap paso a paso"}]}, "style": {"background": "#ffffff", "text_color": "#111111"}},
            {"type": "form", "order": 3, "visible": True, "content": {"headline": "Descarga tu copia gratuita", "subheadline": "Ingresa tus datos y te la enviamos al instante", "fields": ["name", "email", "whatsapp"], "cta_text": "ENVIAR MI GUÍA →", "success_message": "¡Listo! Revisa tu WhatsApp para descargar 📱"}, "style": {"background": "#f8f9fa", "text_color": "#111111", "cta_color": "#f59e0b"}}
        ],
        "theme": {"primary_color": "#f59e0b", "secondary_color": "#6366f1", "background_color": "#ffffff", "text_color": "#111111", "font_heading": "Montserrat", "font_body": "Inter"}
    }
}


# ─── CRUD Endpoints ─────────────────────────────────────────────────────────

@router.get("/campaigns/{campaign_id}/landing-pages")
async def list_landing_pages(campaign_id: str, request: Request):
    _validate_auth(request, campaign_id)
    sb = _sb()
    r = sb.table("landing_pages").select("*").eq("campaign_id", campaign_id).neq("status", "archived").order("created_at", desc=True).execute()
    return {"ok": True, "data": r.data or []}


@router.post("/campaigns/{campaign_id}/landing-pages")
async def create_landing_page(campaign_id: str, request: Request):
    _validate_auth(request, campaign_id)
    body = await request.json()
    sb = _sb()

    # Get campaign for org_id
    camp = sb.table("campaigns").select("org_id, event_name").eq("id", campaign_id).limit(1).execute()
    org_id = (camp.data or [{}])[0].get("org_id", "")
    event_name = (camp.data or [{}])[0].get("event_name", "Mi Evento")

    template_id = body.get("template_id", "evento_presencial")
    template = TEMPLATES.get(template_id, TEMPLATES["evento_presencial"])

    # Generate slug
    slug = body.get("slug") or f"{event_name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    slug = "".join(c for c in slug if c.isalnum() or c in "-_").lower()[:60]

    # Pre-fill sections from template, inject event_name
    sections = json.loads(json.dumps(template["sections"]))
    for s in sections:
        if s["type"] == "hero" and s["content"].get("headline"):
            s["content"]["headline"] = s["content"]["headline"].replace("tu Vida", f"tu Vida en {event_name}")

    row = {
        "org_id": org_id,
        "campaign_id": campaign_id,
        "title": body.get("title", event_name),
        "slug": slug,
        "template_id": template_id,
        "sections": sections,
        "theme": template["theme"],
        "meta_pixel_id": body.get("meta_pixel_id", ""),
        "google_tag_id": body.get("google_tag_id", ""),
        "tiktok_pixel_id": body.get("tiktok_pixel_id", ""),
        "og_title": body.get("og_title", event_name),
        "og_description": body.get("og_description", ""),
        "og_image": body.get("og_image", ""),
        "status": "draft",
    }

    r = sb.table("landing_pages").insert(row).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.get("/landing-pages/{page_id}")
async def get_landing_page(page_id: str, request: Request):
    """Get a single landing page by ID."""
    sb = _sb()
    r = sb.table("landing_pages").select("*").eq("id", page_id).limit(1).execute()
    page = (r.data or [None])[0]
    if not page:
        raise HTTPException(status_code=404, detail="Landing page not found")
    _validate_auth(request, page.get("campaign_id", ""))
    return {"ok": True, "data": page}


@router.patch("/landing-pages/{page_id}")
async def update_landing_page(page_id: str, request: Request):
    body = await request.json()
    sb = _sb()

    # Get campaign_id for auth
    existing = sb.table("landing_pages").select("campaign_id").eq("id", page_id).limit(1).execute()
    campaign_id = (existing.data or [{}])[0].get("campaign_id", "")
    _validate_auth(request, campaign_id)

    allowed = {"title", "slug", "sections", "theme", "meta_pixel_id", "google_tag_id",
               "tiktok_pixel_id", "og_title", "og_description", "og_image", "custom_domain",
               "status", "form_id", "template_id"}
    updates = {k: v for k, v in body.items() if k in allowed}
    updates["updated_at"] = "now()"

    r = sb.table("landing_pages").update(updates).eq("id", page_id).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/landing-pages/{page_id}")
async def delete_landing_page(page_id: str, request: Request):
    sb = _sb()
    existing = sb.table("landing_pages").select("campaign_id").eq("id", page_id).limit(1).execute()
    campaign_id = (existing.data or [{}])[0].get("campaign_id", "")
    _validate_auth(request, campaign_id)

    sb.table("landing_pages").update({"status": "archived"}).eq("id", page_id).execute()
    return {"ok": True}


# ─── Public Render Endpoint ─────────────────────────────────────────────────

@router.get("/landing-pages/render/{slug}")
async def render_landing_page(slug: str, preview: Optional[str] = Query(None)):
    """Public endpoint — returns landing page data for the Next.js renderer.
    ?preview=true allows viewing draft pages (used by the visual editor iframe).
    """
    sb = _sb()
    r = sb.table("landing_pages").select(
        "id, campaign_id, title, slug, sections, theme, "
        "meta_pixel_id, google_tag_id, tiktok_pixel_id, "
        "og_title, og_description, og_image, status"
    ).eq("slug", slug).limit(1).execute()

    page = (r.data or [None])[0]
    is_preview = preview == "true"
    allowed_statuses = ("published", "draft") if is_preview else ("published",)
    if not page or page.get("status") not in allowed_statuses:
        raise HTTPException(status_code=404, detail="Page not found")

    # Get campaign info for the form
    campaign_id = page.get("campaign_id", "")
    camp = sb.table("campaigns").select(
        "id, event_name, event_date, event_location, twilio_whatsapp_from"
    ).eq("id", campaign_id).limit(1).execute()
    campaign = (camp.data or [{}])[0]

    return {
        "ok": True,
        "page": page,
        "campaign": campaign,
    }


# ─── Templates List ─────────────────────────────────────────────────────────

@router.get("/landing-pages/templates")
async def list_templates():
    """List available landing page templates."""
    return {
        "ok": True,
        "data": [
            {"id": k, "name": v["name"], "description": v["description"], "section_count": len(v["sections"])}
            for k, v in TEMPLATES.items()
        ]
    }


# ─── AI Generation ──────────────────────────────────────────────────────────

@router.post("/landing-pages/generate")
async def generate_landing_page(request: Request):
    """AI-powered landing page generation from natural language prompt."""
    body = await request.json()
    campaign_id = body.get("campaign_id", "")
    prompt = body.get("prompt", "")
    current_sections = body.get("current_sections")

    if campaign_id:
        _validate_auth(request, campaign_id)

    sb = _sb()

    # Get campaign context
    campaign = {}
    if campaign_id:
        r = sb.table("campaigns").select("event_name, event_date, event_location, event_speakers, stripe_price_ids").eq("id", campaign_id).limit(1).execute()
        campaign = (r.data or [{}])[0]

    # Build system prompt
    system_prompt = f"""Eres un experto en diseño de landing pages de alta conversión para el mercado LATAM.
Tu trabajo es generar o modificar secciones de una landing page en formato JSON.

Datos de la campaña:
- Evento: {campaign.get('event_name', 'Mi Evento')}
- Fecha: {campaign.get('event_date', 'Por confirmar')}
- Lugar: {campaign.get('event_location', 'Por confirmar')}
- Speakers: {campaign.get('event_speakers', 'Por confirmar')}
- Precios VIP: {json.dumps(campaign.get('stripe_price_ids', {{}}))}

TIPOS DE SECCIÓN DISPONIBLES: hero, countdown, benefits, speakers, testimonials, video, form, faq, cta, custom

SCHEMA DETALLADO DE CADA TIPO DE SECCIÓN:

hero:
  content: {{headline, subheadline, cta_text, cta_url, video_url, background_image}}
  style: {{background, text_color, cta_color}}

countdown:
  content: {{headline, target_date}}  (target_date en ISO formato)
  style: {{background, text_color}}

benefits:
  content: {{headline, items: [{{icon, title, description}}]}}
  style: {{background, text_color}}

speakers:
  content: {{headline, speakers: [{{name, title, image_url, bio}}]}}
  style: {{background, text_color}}

testimonials:
  content: {{headline, items: [{{name, text, image_url}}]}}
  style: {{background, text_color}}

video:
  content: {{headline, video_url, description}}
  style: {{background, text_color}}

form:
  content: {{headline, subheadline, fields: ["name","email","whatsapp"], cta_text, success_message}}
  style: {{background, text_color, cta_color}}

faq:
  content: {{headline, items: [{{question, answer}}]}}
  style: {{background, text_color}}

cta:
  content: {{headline, subheadline, cta_text, cta_url}}
  style: {{background, text_color, cta_color}}

custom:
  content: {{html}}
  style: {{background, text_color}}

REGLAS CRÍTICAS:
1. SIGUE AL PIE DE LA LETRA las instrucciones del usuario. Si dice "cambia el headline a X", usa EXACTAMENTE el texto X, no lo parafrasees ni lo mejores.
2. Si el usuario dice un color específico (ej "dorado", "rojo"), usa ese color exacto.
3. Si el usuario pide agregar/quitar/mover una sección específica, hazlo EXACTAMENTE como pide.
4. Si el usuario proporciona URLs de imágenes o videos, úsalas EXACTAMENTE en los campos correspondientes (image_url, video_url, background_image).
5. Cuando modificas secciones existentes, PRESERVA todo el contenido que el usuario NO pidió cambiar. No borres ni cambies cosas que no se pidieron.
6. Mantén TODOS los campos de cada sección, incluso los vacíos. No omitas campos del schema.
7. El order de las secciones debe ser secuencial empezando en 1.
8. Cada sección DEBE tener un campo "id" como string único (usa tipo-orden, ej "hero-1", "benefits-3").

Responde SOLO con JSON válido. El formato debe ser:
{{"sections": [...], "theme": {{"primary_color": "...", "secondary_color": "...", "background_color": "...", "text_color": "...", "font_heading": "Montserrat", "font_body": "Inter"}}}}

Optimiza para:
- Móvil primero (la mayoría del tráfico es mobile en LATAM)
- Urgencia y escasez en el copy
- CTAs claros y llamativos
- Colores que generen confianza y emoción
- Textos en español latinoamericano"""

    user_msg = prompt
    if current_sections:
        user_msg = f"Secciones actuales:\n{json.dumps(current_sections, ensure_ascii=False)}\n\nPedido del usuario: {prompt}"

    # Call OpenAI
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.4,
                }
            )
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            return {"ok": True, "data": result}
    except Exception as exc:
        logger.exception("ai_generation_failed")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(exc)[:200]}")
