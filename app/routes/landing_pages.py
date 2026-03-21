"""Landing page builder endpoints."""
import logging
import uuid
import json
import time
import random
import string
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Query, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..settings import settings

logger = logging.getLogger("landing_pages")
router = APIRouter(prefix="/v1", tags=["landing-pages"])

# ─── In-memory job store for async AI generation ────────────────────────────
# Jobs expire after 10 minutes. This avoids Fly.io proxy timeout issues.
_JOBS: dict[str, dict] = {}
_JOB_TTL = 600  # seconds

def _cleanup_old_jobs():
    """Remove jobs older than TTL."""
    now = time.time()
    expired = [jid for jid, j in _JOBS.items() if now - j.get("created_at", 0) > _JOB_TTL]
    for jid in expired:
        del _JOBS[jid]


def _build_system_prompt(event_name, event_date, event_location, event_speakers,
                         price_info, vip_price, event_description, event_agenda, whatsapp,
                         *, is_edit: bool = False):
    """Build the AI system prompt for landing page generation."""

    campaign_block = f"""Campaign data (use when relevant):
- Name: {event_name}
- Date: {event_date}
- Location: {event_location}
- Speakers: {event_speakers}
- Pricing: {price_info}
{f'- VIP Price: {vip_price}' if vip_price else ''}
{f'- Description: {event_description[:500]}' if event_description else ''}
{f'- Agenda: {event_agenda[:500]}' if event_agenda else ''}
{f'- WhatsApp: {whatsapp}' if whatsapp else ''}"""

    section_schema = """Available section types and their schemas:

hero: content={headline, subheadline, cta_text, cta_url, video_url, video_autoplay, video_controls, video_muted, video_loop, background_image}, style={background, text_color, cta_color}
  - background_image accepts image URLs (.jpg/.png) AND video URLs (.mp4/.webm). If video, plays as background.

countdown: content={headline, target_date (ISO)}, style={background, text_color}

benefits: content={headline, items:[{icon, title, description}]}, style={background, text_color}

speakers: content={headline, subheadline, layout, speakers:[{name, title, image_url, bio}]}, style={background, text_color}
  - layout: "circle" (default), "card" (rectangular photo), "card-bg" (photo as BACKGROUND), "horizontal" (photo left)

testimonials: content={headline, items:[{name, text, image_url}]}, style={background, text_color}

video: content={headline, video_url, description, video_autoplay, video_controls, video_muted, video_loop}, style={background, text_color}

form: content={headline, subheadline, fields:["name","email","whatsapp"], cta_text, success_message}, style={background, text_color, cta_color}

faq: content={headline, items:[{question, answer}]}, style={background, text_color}

cta: content={headline, subheadline, cta_text, cta_url}, style={background, text_color, cta_color}

pricing: content={headline, subheadline, guarantee, tiers:[{name, price, description, features:[], cta_text, cta_url, highlighted:bool}]}, style={background, text_color, cta_color}

custom: content={html}, style={background, text_color}
  - ONLY use custom if no other type fits. Always prefer native types."""

    edit_rules = """
## ABSOLUTE PRIORITY: THE USER'S INSTRUCTION IS LAW

You MUST follow the user's instruction EXACTLY, word by word. The user's instruction is the ONLY thing that matters. Do not optimize, improve, or second-guess what they asked for. If they say "change X to Y", change X to Y and NOTHING else.

## CRITICAL EDITING RULES — FOLLOW THESE OR THE PAGE WILL BREAK

You are making SURGICAL EDITS to an existing landing page. The user's current sections JSON is provided below their instruction.

### GOLDEN RULE: Copy-paste everything you don't change.

Your output must contain EVERY section from the input. If the user asked to change one section, the other sections must be BIT-FOR-BIT IDENTICAL to the input. Do NOT:
- Rephrase, reword, or "improve" any text the user didn't mention
- Remove ANY section (even if you think it's redundant)
- Add sections the user didn't ask for
- Change colors, fonts, or styles unless explicitly asked
- Reorder sections unless explicitly asked
- Translate or change the language of existing content
- Summarize, shorten, or rewrite existing copy

### STEP-BY-STEP PROCESS (follow this exactly):

1. Read the user's instruction. Break it into individual requests (e.g., "change headline AND move form" = 2 requests).
2. For each request, identify: which section type? which field? what is the new value?
3. Build the output JSON:
   - Start by copying ALL input sections exactly as-is into the output
   - Then apply ONLY the identified changes to the specific fields
   - Verify: does every input section appear in the output? If not, you dropped one — add it back.
4. Count: input had N sections. Your output must have N sections (plus any the user asked to add, minus any they asked to remove).

### COMMON MISTAKES TO AVOID:
- User says "change the headline" → You change the headline BUT ALSO rewrite subheadline. WRONG. Only change what was asked.
- User says "add a headline" → You add the headline but remove another section. WRONG. Add means add, not replace.
- User says "move X above Y" → You move them but also rewrite their content. WRONG. Only reorder.
- User gives text in quotes → You paraphrase it. WRONG. Use their EXACT words.
- User says "change the color to red" → You change the color AND also rewrite the text. WRONG.
- User asks about ONE section → You "improve" other sections while you're at it. WRONG. NEVER touch untouched sections.

### IMAGE & VIDEO RULES:
- "de fondo" / "background" → background_image field
- "foto del speaker" → image_url inside speaker object
- "sin controles" → video_controls: false, video_muted: true
- "autoplay" → video_autoplay: true, video_muted: true
- "video de fondo" → put video URL in background_image of hero

### LANGUAGE RULES:
- ALWAYS respond in the same language the user writes in
- If the page content is in Spanish, keep it in Spanish
- If the user writes in Spanish, all new content must be in Spanish
- NEVER translate existing content to another language""" if is_edit else """
## GENERATION MODE

You are creating a NEW landing page from scratch. Make it STUNNING — this should look like it was designed by a premium agency, not a template.

### ABSOLUTE PRIORITY: THE USER'S INSTRUCTION IS LAW
Follow the user's prompt EXACTLY. If they describe specific sections, colors, layout, or text — use exactly what they said. Do not substitute your own ideas for theirs. Only fill gaps they didn't specify.

### PAGE STRUCTURE (create 8-12 sections for a complete page):
1. Hero — powerful headline + subheadline + CTA with gradient background
2. Social proof / stats — numbers that build credibility (attendees, countries, years, etc.)
3. Benefits — 3-6 compelling reasons to attend/buy with icons
4. Speakers / Team — with authority-building bios
5. Testimonials — 3+ specific, believable testimonials
6. Video (if relevant) — embed section for pitch video
7. Pricing — with highlighted recommended tier
8. FAQ — 4-6 common objections answered
9. Form — short registration form with value-reinforcing headline
10. Final CTA — urgency-driven closing section

### CONTENT RULES:
- If the user specifies text in quotes, use their EXACT words
- Write in the same language the user uses — if Spanish, use engaging Latin American tone
- Use real campaign data (event name, speakers, dates) — do NOT use placeholder text like "Lorem ipsum" or "Speaker Name"
- Every section must have REAL, specific content — not generic filler
- Alternate section background colors/gradients for visual rhythm"""

    return f"""You are a world-class landing page designer known for creating stunning, high-converting pages. You are NOT a generic AI — you produce PREMIUM, visually striking designs that rival the best agencies. You output JSON that defines landing page sections and theme.

{campaign_block}

{section_schema}

## DESIGN PHILOSOPHY — THIS IS WHAT MAKES YOUR PAGES PREMIUM

### Visual Identity:
- Use RICH, DEEP color palettes — not flat boring colors. Think gradients like "linear-gradient(135deg, #667eea 0%, #764ba2 100%)" or "linear-gradient(160deg, #0f0c29 0%, #302b63 50%, #24243e 100%)"
- Dark themes should feel LUXURIOUS (deep navy, charcoal, midnight purple) — not just "#000000"
- Light themes should feel CLEAN and AIRY (off-white backgrounds, subtle shadows, refined typography)
- Every section should have a DISTINCT visual personality while maintaining cohesion
- Use contrasting accent colors for CTAs (vibrant green, electric blue, warm orange) that POP against the background

### Typography:
- font_heading: Use premium fonts like "Playfair Display", "DM Serif Display", "Outfit", "Space Grotesk", "Sora", "Cabinet Grotesk"
- font_body: Use clean, readable fonts like "Inter", "DM Sans", "Plus Jakarta Sans", "Satoshi", "General Sans"
- NEVER use generic fonts like "Arial" or "Helvetica"

### Copywriting:
- Write POWERFUL, emotional headlines that create urgency and desire
- Use psychological triggers: scarcity, social proof, authority, FOMO
- Subheadlines should ADD context, not repeat the headline
- CTAs should be action-oriented and specific: "RESERVA TU LUGAR AHORA →", "QUIERO MI ENTRADA VIP 🔥", "SÍ, QUIERO TRANSFORMAR MI NEGOCIO"
- Use emojis strategically in headlines for visual impact (🔥 🚀 ⚡ 💰 🎯)
- Write in the user's language with LOCAL flair — if Spanish, use Latin American conversational tone

### Section Design:
- Hero: ALWAYS include a compelling subheadline AND a strong CTA. Use gradient backgrounds.
- Benefits: Use meaningful icons (emojis work great), 3-6 items with punchy titles
- Speakers: Include bios that establish AUTHORITY and CREDIBILITY
- Testimonials: Make them feel REAL and specific, not generic praise
- Form: Keep it SHORT (name, email, whatsapp max). The headline should reinforce the VALUE they're getting.
- Pricing: Use psychological pricing (anchoring, highlighted tier, "MOST POPULAR" badge)
- CTA sections: Create URGENCY — limited spots, deadline, bonus expiring

{edit_rules}

## OUTPUT FORMAT

Return ONLY valid JSON (no markdown, no explanation, no commentary):
{{"sections": [...], "theme": {{"primary_color": "...", "background_color": "...", "text_color": "...", "font_heading": "...", "font_body": "..."}}}}

Each section: {{"id": "type-N", "type": "type", "order": N, "visible": true, "content": {{...}}, "style": {{...}}}}
- order: sequential from 1
- id: unique per section (e.g. "hero-1", "speakers-1")"""

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
    },
    "vsl": {
        "name": "VSL (Video Sales Letter)",
        "category": "ventas",
        "description": "Página de ventas con video, filtro de audiencia, múltiples CTAs y agendador",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Tu negocio no está fallando por falta de clientes.", "subheadline": "Está fallando porque no tienes un sistema para generarlos y convertirlos.", "cta_text": "APLICAR PARA UNA LLAMADA →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(180deg, #1a1a2e 0%, #16213e 100%)", "text_color": "#ffffff", "cta_color": "#2563eb"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "Si hoy:", "items": [{"icon": "⚠️", "title": "Dependes de referidos", "description": ""}, {"icon": "⚠️", "title": "Tus anuncios no convierten", "description": ""}, {"icon": "⚠️", "title": "No tienes un proceso claro de ventas", "description": ""}, {"icon": "⚠️", "title": "Tu crecimiento es inconsistente", "description": ""}]}, "style": {"background": "#16213e", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 3, "visible": True, "content": {"headline": "Tu problema no es marketing.", "subheadline": "Es que no tienes una estructura que convierta ese marketing en clientes. En este video te explico exactamente cómo solucionarlo.", "cta_text": "", "cta_url": ""}, "style": {"background": "#1a1a2e", "text_color": "#ffffff"}},
            {"id": "video-1", "type": "video", "order": 4, "visible": True, "content": {"headline": "", "video_url": "", "description": "En este video vas a entender: por qué no escalas online, cuál es el error estructural clave, y cómo construir un sistema real.", "video_autoplay": True, "video_controls": True, "video_muted": False, "video_loop": False}, "style": {"background": "#0f0f23", "text_color": "#ffffff"}},
            {"id": "benefits-2", "type": "benefits", "order": 5, "visible": True, "content": {"headline": "Esto NO es para todos.", "items": [{"icon": "✅", "title": "Tienes un negocio activo", "description": ""}, {"icon": "✅", "title": "Ya generas ingresos", "description": ""}, {"icon": "✅", "title": "Quieres escalar en serio", "description": ""}]}, "style": {"background": "#16213e", "text_color": "#ffffff"}},
            {"id": "cta-2", "type": "cta", "order": 6, "visible": True, "content": {"headline": "En la llamada vamos a:", "subheadline": "Analizar tu negocio • Identificar qué te frena • Ver si podemos ayudarte", "cta_text": "APLICAR AHORA →", "cta_url": "#form"}, "style": {"background": "#1a1a2e", "text_color": "#ffffff", "cta_color": "#dc2626"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "Agenda tu llamada de diagnóstico", "subheadline": "Selecciona un horario y te contactamos", "fields": ["name", "email", "whatsapp"], "cta_text": "AGENDAR MI LLAMADA →", "success_message": "¡Listo! Te contactaremos por WhatsApp para confirmar tu sesión."}, "style": {"background": "linear-gradient(180deg, #1a1a2e 0%, #0f0f23 100%)", "text_color": "#ffffff", "cta_color": "#2563eb"}}
        ],
        "theme": {"primary_color": "#2563eb", "secondary_color": "#dc2626", "background_color": "#0f0f23", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "checkout_page": {
        "name": "Checkout / Sales Page",
        "category": "ventas",
        "description": "Página de ventas con pricing, testimonios, garantía y checkout",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Transforma tu negocio con nuestro programa", "subheadline": "El sistema completo para escalar tus ventas online", "cta_text": "VER PLANES →", "cta_url": "#pricing", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #0c0c1d 0%, #1a1a3e 100%)", "text_color": "#ffffff", "cta_color": "#f59e0b"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "Lo que incluye:", "items": [{"icon": "📦", "title": "Módulo 1: Fundamentos", "description": "Construye las bases de tu sistema de ventas"}, {"icon": "🎯", "title": "Módulo 2: Estrategia", "description": "Define tu plan de crecimiento personalizado"}, {"icon": "🚀", "title": "Módulo 3: Escala", "description": "Automatiza y multiplica tus resultados"}, {"icon": "🤝", "title": "Soporte 1-a-1", "description": "Acceso directo a nuestro equipo de expertos"}]}, "style": {"background": "#111122", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 3, "visible": True, "content": {"headline": "Resultados reales de nuestros alumnos", "items": [{"name": "Carlos M.", "text": "En 3 meses tripliqué mis ventas online", "image_url": ""}, {"name": "Ana R.", "text": "El mejor programa que he tomado. ROI de 10x en 60 días.", "image_url": ""}, {"name": "Roberto L.", "text": "Finalmente tengo un sistema que funciona sin depender de mí.", "image_url": ""}]}, "style": {"background": "#0c0c1d", "text_color": "#ffffff"}},
            {"id": "faq-1", "type": "faq", "order": 4, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Hay garantía?", "answer": "Sí, tienes 30 días de garantía. Si no ves resultados, te devolvemos tu dinero."}, {"question": "¿Cuánto tiempo tengo acceso?", "answer": "Acceso de por vida a todos los módulos y actualizaciones."}, {"question": "¿Es para mi tipo de negocio?", "answer": "Funciona para cualquier negocio que venda productos o servicios online."}]}, "style": {"background": "#111122", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 5, "visible": True, "content": {"headline": "¿Listo para transformar tu negocio?", "subheadline": "Únete a los cientos de emprendedores que ya están escalando sus ventas", "cta_text": "COMENZAR AHORA →", "cta_url": "#form"}, "style": {"background": "linear-gradient(135deg, #0c0c1d 0%, #1a1a3e 100%)", "text_color": "#ffffff", "cta_color": "#f59e0b"}},
            {"id": "form-1", "type": "form", "order": 6, "visible": True, "content": {"headline": "Empieza hoy", "subheadline": "Regístrate y te contactamos para comenzar", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO EMPEZAR →", "success_message": "¡Excelente! Te contactaremos por WhatsApp con los siguientes pasos."}, "style": {"background": "#0c0c1d", "text_color": "#ffffff", "cta_color": "#f59e0b"}}
        ],
        "theme": {"primary_color": "#f59e0b", "secondary_color": "#8b5cf6", "background_color": "#0c0c1d", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "webinar_vip": {
        "name": "Webinar VIP",
        "category": "eventos",
        "description": "Webinar con countdown, bonus exclusivo y urgencia",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Masterclass EXCLUSIVA en Vivo", "subheadline": "Descubre el sistema exacto que usamos para generar $100K/mes en ventas online", "cta_text": "RESERVAR MI LUGAR VIP →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #1e0533 0%, #0a1628 100%)", "text_color": "#ffffff", "cta_color": "#8b5cf6"}},
            {"id": "countdown-1", "type": "countdown", "order": 2, "visible": True, "content": {"headline": "LA MASTERCLASS COMIENZA EN:", "target_date": ""}, "style": {"background": "#0a0a1a", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "Lo que vas a aprender:", "items": [{"icon": "🎯", "title": "El Framework de Ventas", "description": "El sistema paso a paso para convertir extraños en clientes"}, {"icon": "💰", "title": "Estrategia de Pricing", "description": "Cómo cobrar lo que vales y que te paguen con gusto"}, {"icon": "📈", "title": "Escala Automática", "description": "Cómo automatizar tu proceso de ventas completo"}]}, "style": {"background": "#0a1628", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Tu instructor", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "CEO & Fundador", "image_url": "", "bio": "Experto en marketing digital y ventas online con más de 10 años de experiencia."}]}, "style": {"background": "#1e0533", "text_color": "#ffffff"}},
            {"id": "benefits-2", "type": "benefits", "order": 5, "visible": True, "content": {"headline": "🎁 BONUS por asistir en vivo:", "items": [{"icon": "📋", "title": "Plantilla de Funnel", "description": "Lista para copiar y usar en tu negocio"}, {"icon": "🎥", "title": "Grabación completa", "description": "Acceso a la grabación si no puedes asistir en vivo"}, {"icon": "💬", "title": "Sesión de Q&A", "description": "Pregunta lo que quieras al final de la masterclass"}]}, "style": {"background": "#0a0a1a", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 6, "visible": True, "content": {"headline": "🔒 Reserva tu lugar VIP", "subheadline": "Cupo limitado a 100 personas — no te quedes fuera", "fields": ["name", "email", "whatsapp"], "cta_text": "RESERVAR LUGAR VIP →", "success_message": "¡Registrado! Te enviaremos el link de acceso por WhatsApp 📱"}, "style": {"background": "linear-gradient(135deg, #1e0533 0%, #0a1628 100%)", "text_color": "#ffffff", "cta_color": "#8b5cf6"}}
        ],
        "theme": {"primary_color": "#8b5cf6", "secondary_color": "#ec4899", "background_color": "#0a0a1a", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "event_upsell": {
        "name": "Evento + Upsell VIP",
        "category": "eventos",
        "description": "Registro a evento con upsell a ticket VIP y comparación de tiers",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "El Evento del Año", "subheadline": "2 días intensivos que cambiarán la dirección de tu negocio para siempre", "cta_text": "QUIERO MI ENTRADA →", "cta_url": "#pricing", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", "text_color": "#ffffff", "cta_color": "#10b981"}},
            {"id": "countdown-1", "type": "countdown", "order": 2, "visible": True, "content": {"headline": "FALTAN:", "target_date": ""}, "style": {"background": "#0a0f1a", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "¿Qué vas a vivir?", "items": [{"icon": "🎤", "title": "10+ Speakers Internacionales", "description": "Los mejores expertos en su campo"}, {"icon": "🤝", "title": "Networking Premium", "description": "Conecta con 500+ emprendedores"}, {"icon": "📋", "title": "Plan de Acción Personalizado", "description": "Sales con una estrategia clara"}, {"icon": "🎁", "title": "Material Exclusivo", "description": "Workbooks, templates y grabaciones"}]}, "style": {"background": "#111827", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Speakers Confirmados", "layout": "card-bg", "speakers": [{"name": "Speaker 1", "title": "CEO & Fundador", "image_url": "", "bio": ""}, {"name": "Speaker 2", "title": "Director de Ventas", "image_url": "", "bio": ""}, {"name": "Speaker 3", "title": "Experto en Marketing", "image_url": "", "bio": ""}]}, "style": {"background": "#0f172a", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 5, "visible": True, "content": {"headline": "Lo que dicen los asistentes", "items": [{"name": "María G.", "text": "Fue la mejor inversión de mi vida. Recuperé la inversión en 2 semanas.", "image_url": ""}, {"name": "Juan P.", "text": "El networking solo ya vale el precio del ticket VIP.", "image_url": ""}]}, "style": {"background": "#111827", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 6, "visible": True, "content": {"headline": "🔥 Oferta por tiempo limitado", "subheadline": "Precio especial de lanzamiento — sube cada semana", "cta_text": "ASEGURAR MI ENTRADA →", "cta_url": "#form"}, "style": {"background": "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", "text_color": "#ffffff", "cta_color": "#10b981"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "Reserva tu Entrada", "subheadline": "Completa tus datos y asegura tu lugar", "fields": ["name", "email", "whatsapp"], "cta_text": "RESERVAR AHORA →", "success_message": "¡Listo! Te enviaremos todos los detalles por WhatsApp 📱"}, "style": {"background": "#0a0f1a", "text_color": "#ffffff", "cta_color": "#10b981"}},
            {"id": "faq-1", "type": "faq", "order": 8, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Cuál es la diferencia entre General y VIP?", "answer": "El ticket VIP incluye acceso a sesiones exclusivas, networking privado, cena con speakers y material adicional."}, {"question": "¿Puedo pagar en cuotas?", "answer": "Sí, ofrecemos planes de pago a meses. Te lo explicamos por WhatsApp."}, {"question": "¿Qué pasa si no puedo asistir?", "answer": "Puedes transferir tu entrada a otra persona o recibir acceso a las grabaciones."}]}, "style": {"background": "#111827", "text_color": "#ffffff"}}
        ],
        "theme": {"primary_color": "#10b981", "secondary_color": "#f59e0b", "background_color": "#0f172a", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "course_launch": {
        "name": "Lanzamiento de Curso",
        "category": "ventas",
        "description": "Landing para vender cursos online con módulos, instructor y pricing",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Domina [tu habilidad] en 8 semanas", "subheadline": "El programa online más completo para llevar tu negocio al siguiente nivel", "cta_text": "INSCRIBIRME →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #0c0a3e 0%, #1a0533 100%)", "text_color": "#ffffff", "cta_color": "#ec4899"}},
            {"id": "video-1", "type": "video", "order": 2, "visible": True, "content": {"headline": "Mira lo que vas a aprender", "video_url": "", "description": "", "video_autoplay": False, "video_controls": True}, "style": {"background": "#0c0a3e", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "Contenido del programa", "items": [{"icon": "1️⃣", "title": "Módulo 1: Fundamentos", "description": "Semanas 1-2: Construye las bases sólidas"}, {"icon": "2️⃣", "title": "Módulo 2: Estrategia", "description": "Semanas 3-4: Define tu plan de crecimiento"}, {"icon": "3️⃣", "title": "Módulo 3: Implementación", "description": "Semanas 5-6: Ejecuta con nuestro sistema probado"}, {"icon": "4️⃣", "title": "Módulo 4: Escala", "description": "Semanas 7-8: Automatiza y multiplica"}]}, "style": {"background": "#1a0533", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Tu instructor", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Experto en la materia", "image_url": "", "bio": "Con más de X años de experiencia ayudando a emprendedores a alcanzar sus metas."}]}, "style": {"background": "#0c0a3e", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 5, "visible": True, "content": {"headline": "Historias de éxito", "items": [{"name": "Alumno 1", "text": "Este curso cambió completamente mi forma de hacer negocios", "image_url": ""}, {"name": "Alumno 2", "text": "La mejor inversión en educación que he hecho", "image_url": ""}, {"name": "Alumno 3", "text": "En 2 meses ya había recuperado mi inversión 3 veces", "image_url": ""}]}, "style": {"background": "#1a0533", "text_color": "#ffffff"}},
            {"id": "faq-1", "type": "faq", "order": 6, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Cuánto dura el programa?", "answer": "8 semanas de contenido + acceso de por vida a la plataforma y actualizaciones."}, {"question": "¿Necesito experiencia previa?", "answer": "No, el programa está diseñado desde cero. Solo necesitas ganas de aprender."}, {"question": "¿Hay garantía?", "answer": "Sí, 30 días de garantía total. Si no te convence, te devolvemos tu dinero."}]}, "style": {"background": "#0c0a3e", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "Inscríbete ahora", "subheadline": "Precio especial de lanzamiento — cupos limitados", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO INSCRIBIRME →", "success_message": "¡Inscrito! Te contactaremos por WhatsApp con toda la información."}, "style": {"background": "linear-gradient(135deg, #0c0a3e 0%, #1a0533 100%)", "text_color": "#ffffff", "cta_color": "#ec4899"}}
        ],
        "theme": {"primary_color": "#ec4899", "secondary_color": "#8b5cf6", "background_color": "#0c0a3e", "text_color": "#ffffff", "font_heading": "Montserrat", "font_body": "Inter"}
    },
    "appointment_booking": {
        "name": "Agendar Cita / Servicio",
        "category": "servicios",
        "description": "Landing para agendar llamadas, consultas o servicios profesionales",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Agenda tu consulta gratuita", "subheadline": "En 30 minutos analizaremos tu situación y te daremos un plan de acción personalizado", "cta_text": "AGENDAR AHORA →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%)", "text_color": "#0f172a", "cta_color": "#2563eb"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "¿Cómo funciona?", "items": [{"icon": "1️⃣", "title": "Agenda tu cita", "description": "Elige el horario que mejor te funcione"}, {"icon": "2️⃣", "title": "Sesión de diagnóstico", "description": "Analizamos tu situación actual y tus metas"}, {"icon": "3️⃣", "title": "Plan personalizado", "description": "Te entregamos una estrategia clara y accionable"}]}, "style": {"background": "#ffffff", "text_color": "#0f172a"}},
            {"id": "speakers-1", "type": "speakers", "order": 3, "visible": True, "content": {"headline": "¿Con quién hablarás?", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Consultor especializado", "image_url": "", "bio": "Ayudo a empresarios a escalar sus negocios con sistemas probados."}]}, "style": {"background": "#f1f5f9", "text_color": "#0f172a"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 4, "visible": True, "content": {"headline": "Lo que dicen nuestros clientes", "items": [{"name": "Cliente 1", "text": "La sesión de diagnóstico me abrió los ojos. En una semana ya estaba implementando cambios.", "image_url": ""}, {"name": "Cliente 2", "text": "Profesional, directo y con un plan claro. Exactamente lo que necesitaba.", "image_url": ""}]}, "style": {"background": "#ffffff", "text_color": "#0f172a"}},
            {"id": "cta-1", "type": "cta", "order": 5, "visible": True, "content": {"headline": "Esto no es para todos.", "subheadline": "Trabajamos con empresarios que ya tienen un negocio activo y quieren escalar. Si estás empezando desde cero, este proceso no es para ti.", "cta_text": "SOY EL PERFIL CORRECTO →", "cta_url": "#form"}, "style": {"background": "#f1f5f9", "text_color": "#0f172a", "cta_color": "#2563eb"}},
            {"id": "form-1", "type": "form", "order": 6, "visible": True, "content": {"headline": "Agenda tu sesión", "subheadline": "La disponibilidad es limitada. Las sesiones se asignan por orden de agenda.", "fields": ["name", "email", "whatsapp"], "cta_text": "AGENDAR MI SESIÓN →", "success_message": "¡Agendado! Te contactaremos por WhatsApp para confirmar tu horario."}, "style": {"background": "#ffffff", "text_color": "#0f172a", "cta_color": "#2563eb"}}
        ],
        "theme": {"primary_color": "#2563eb", "secondary_color": "#0ea5e9", "background_color": "#ffffff", "text_color": "#0f172a", "font_heading": "Inter", "font_body": "Inter"}
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

    # Get campaign for org_id and tracking pixels
    camp = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
    camp_data = (camp.data or [{}])[0]
    org_id = camp_data.get("org_id", "")
    event_name = camp_data.get("event_name", "Mi Evento")

    # Support custom sections/theme (from AI generation) OR template
    custom_sections = body.get("sections")
    custom_theme = body.get("theme")

    template_id = body.get("template_id") or body.get("template") or "evento_presencial"
    # Normalize template_id (frontend sends "evento-presencial", backend uses "evento_presencial")
    template_id = template_id.replace("-", "_")
    template = TEMPLATES.get(template_id, TEMPLATES["evento_presencial"])

    # Generate slug
    slug = body.get("slug") or f"{event_name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    slug = "".join(c for c in slug if c.isalnum() or c in "-_").lower()[:60]

    # Use custom sections if provided (from AI), otherwise use template
    if custom_sections and isinstance(custom_sections, list) and len(custom_sections) > 0:
        sections = custom_sections
        # Add IDs if missing
        for i, s in enumerate(sections):
            if not s.get("id"):
                s["id"] = f"{s.get('type', 'section')}-{i+1}"
    else:
        sections = json.loads(json.dumps(template["sections"]))
        for s in sections:
            if s["type"] == "hero" and s["content"].get("headline"):
                s["content"]["headline"] = s["content"]["headline"].replace("tu Vida", f"tu Vida en {event_name}")

    theme = custom_theme if custom_theme else template["theme"]

    row = {
        "org_id": org_id,
        "campaign_id": campaign_id,
        "title": body.get("title", event_name),
        "slug": slug,
        "template_id": template_id if not custom_sections else "ai_generated",
        "sections": sections,
        "theme": theme,
        "meta_pixel_id": body.get("meta_pixel_id") or camp_data.get("meta_pixel_id", ""),
        "google_tag_id": body.get("google_tag_id") or camp_data.get("google_tag_id", ""),
        "tiktok_pixel_id": body.get("tiktok_pixel_id") or camp_data.get("tiktok_pixel_id", ""),
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


# ─── Job polling endpoint ──────────────────────────────────────────────────

@router.get("/landing-pages/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Poll for async AI generation job status."""
    _cleanup_old_jobs()
    job = _JOBS.get(job_id)
    if not job:
        logger.warning(f"Job {job_id} not found. Active jobs: {list(_JOBS.keys())}")
        raise HTTPException(status_code=404, detail="Job not found or expired")
    logger.info(f"Job {job_id} polled: status={job['status']}")
    return {"ok": True, "job_id": job_id, "status": job["status"], "data": job.get("data"), "error": job.get("error")}


# ─── AI Generation (async job pattern) ────────────────────────────────────

async def _run_generate_job(job_id: str, campaign_id: str, prompt: str, current_sections, chat_history: list = None):
    """Background task: call AI and store result in _JOBS."""
    logger.info(f"Generate job {job_id} started, campaign={campaign_id}")
    try:
        sb = _sb()
        campaign = {}
        if campaign_id:
            r = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
            campaign = (r.data or [{}])[0]

        result = await _call_openai_generate(campaign, prompt, current_sections, chat_history or [])
        logger.info(f"Generate job {job_id}: DONE, {len(result.get('sections', []))} sections")
        _JOBS[job_id] = {**_JOBS[job_id], "status": "done", "data": result}
    except Exception as exc:
        logger.exception(f"Generate job {job_id} failed: {exc}")
        _JOBS[job_id] = {**_JOBS[job_id], "status": "error", "error": str(exc)[:300]}


async def _call_openai_generate(campaign: dict, prompt: str, current_sections, chat_history: list = None) -> dict:
    """Actual AI call for generation."""
    event_name = campaign.get('event_name', 'Mi Evento')
    event_date = campaign.get('event_date', 'Por confirmar')
    event_location = campaign.get('event_location', 'Por confirmar')
    event_speakers = campaign.get('event_speakers', 'Por confirmar')
    price_info = json.dumps(campaign.get('stripe_price_ids') or 'N/A')
    vip_price = campaign.get('vip_price_display', '')
    event_description = campaign.get('event_description', '')
    event_agenda = campaign.get('event_agenda', '')
    whatsapp = campaign.get('whatsapp_number', '')

    is_edit = bool(current_sections)
    system_prompt = _build_system_prompt(event_name, event_date, event_location, event_speakers,
                                         price_info, vip_price, event_description, event_agenda, whatsapp,
                                         is_edit=is_edit)

    # Truncate very long prompts (generous limit for Opus)
    MAX_PROMPT = 15000
    if len(prompt) > MAX_PROMPT:
        prompt = prompt[:14000] + "\n\n... [contenido recortado por longitud] ...\n\n" + prompt[-1000:]

    if current_sections:
        # Put instruction FIRST so Claude focuses on it, then sections as reference
        user_msg = f"""## USER INSTRUCTION — This is what the user wants. Follow it EXACTLY:
{prompt}

## CURRENT PAGE JSON — The page currently has {len(current_sections)} sections. Your output MUST also have {len(current_sections)} sections (unless the user explicitly asked to add or remove one). Copy unchanged sections EXACTLY as-is, character by character:
{json.dumps(current_sections, ensure_ascii=False)}

REMINDER: Return ALL {len(current_sections)} sections. Only modify what the user asked for. Do NOT remove, rephrase, or rewrite anything else."""
    else:
        user_msg = prompt

    import httpx
    logger.info(f"AI generate: system={len(system_prompt)} chars, user={len(user_msg)} chars, history={len(chat_history or [])}")

    # Build conversation messages with history
    # Keep last 10 exchanges max to avoid context bloat
    history = (chat_history or [])[-20:]  # 20 messages = ~10 exchanges
    messages = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            # Truncate old assistant messages (they contain huge JSON) to save tokens
            if role == "assistant" and len(content) > 200:
                content = content[:200] + "... [previous response truncated]"
            messages.append({"role": role, "content": content})
    # Add the current user message
    messages.append({"role": "user", "content": user_msg})
    # Assistant prefill forces Claude to start with JSON immediately
    messages.append({"role": "assistant", "content": "{"})

    # Use Claude (Anthropic) if API key available, otherwise fall back to OpenAI
    if settings.anthropic_api_key:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-opus-4-20250514",
                    "max_tokens": 16384,
                    "temperature": 0.2,
                    "system": system_prompt,
                    "messages": messages,
                }
            )
            if r.status_code != 200:
                logger.error(f"Claude returned {r.status_code}: {r.text[:500]}")
                raise Exception(f"Claude error: {r.status_code}")
            data = r.json()
            # Prepend the "{" we used as prefill since Claude continues from there
            content = "{" + data["content"][0]["text"]
            stop_reason = data.get("stop_reason", "unknown")
            logger.info(f"Claude stop_reason={stop_reason}, content_len={len(content)}")
            if stop_reason == "max_tokens":
                logger.warning("Claude response was TRUNCATED (stop_reason=max_tokens)")
    else:
        # Fallback to OpenAI — build messages with history
        oai_messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                if role == "assistant" and len(content) > 200:
                    content = content[:200] + "... [previous response truncated]"
                oai_messages.append({"role": role, "content": content})
        oai_messages.append({"role": "user", "content": user_msg})
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o",
                    "messages": oai_messages,
                    "response_format": {"type": "json_object"},
                    "temperature": 0.15,
                    "max_tokens": 16384,
                }
            )
            if r.status_code != 200:
                logger.error(f"OpenAI returned {r.status_code}: {r.text[:500]}")
                raise Exception(f"OpenAI error: {r.status_code}")
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            stop_reason = data["choices"][0].get("finish_reason", "unknown")
            logger.info(f"OpenAI finish_reason={stop_reason}, content_len={len(content)}")

    # Parse JSON response
    # Claude may wrap JSON in ```json ... ``` blocks, strip those
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error(f"AI returned invalid JSON: {cleaned[:500]}")
        raise Exception("La IA devolvió una respuesta inválida. Intenta de nuevo.")
    logger.info(f"Parsed keys: {list(parsed.keys())}, sections count: {len(parsed.get('sections', []))}")
    return parsed


@router.post("/landing-pages/generate")
async def generate_landing_page(request: Request):
    """AI-powered landing page generation — returns job_id for polling."""
    body = await request.json()
    campaign_id = body.get("campaign_id", "")
    prompt = body.get("prompt", "")
    current_sections = body.get("current_sections")
    chat_history = body.get("chat_history", [])  # [{role: "user"|"assistant", content: "..."}]

    if campaign_id:
        _validate_auth(request, campaign_id)

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"status": "processing", "created_at": time.time()}

    # Fire and forget — the background task updates _JOBS when done
    logger.info(f"Starting generate job {job_id} for campaign {campaign_id}")
    asyncio.ensure_future(_run_generate_job(job_id, campaign_id, prompt, current_sections, chat_history))

    return {"ok": True, "job_id": job_id, "status": "processing"}


# ─── Clone from URL (async job pattern) ──────────────────────────────────────

async def _run_clone_job(job_id: str, url: str, campaign_id: str):
    """Background task: scrape URL, call OpenAI, store result in _JOBS."""
    logger.info(f"Clone job {job_id} started for URL: {url}")
    try:
        import httpx
        import re
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin

        sb = _sb()
        campaign = {}
        if campaign_id:
            r = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
            campaign = (r.data or [{}])[0]

        # 1. Fetch the URL
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; 2ClicksBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            })
            html = resp.text

        # 2. Parse with BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        extracted = {"title": "", "description": "", "colors": [], "fonts": [], "headings": [],
                     "images": [], "cta_texts": [], "has_video": False, "has_form": False,
                     "has_testimonials": False, "has_faq": False, "has_pricing": False}

        title_tag = soup.find("title")
        if title_tag:
            extracted["title"] = title_tag.get_text(strip=True)[:200]

        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            extracted["description"] = (meta_desc.get("content") or "")[:300]

        colors_found = set()
        for style_tag in soup.find_all("style"):
            text = style_tag.get_text()
            colors_found.update(re.findall(r'#[0-9a-fA-F]{3,8}', text)[:20])
            colors_found.update(re.findall(r'rgb\([^)]+\)', text)[:10])
        theme_color = soup.find("meta", attrs={"name": "theme-color"})
        if theme_color:
            colors_found.add(theme_color.get("content", ""))
        extracted["colors"] = list(colors_found)[:15]

        for link in soup.find_all("link", href=True):
            href = link["href"]
            if "fonts.googleapis.com" in href:
                extracted["fonts"].extend([f.replace("+", " ") for f in re.findall(r'family=([^&:]+)', href)])

        for tag in ["h1", "h2", "h3"]:
            for el in soup.find_all(tag)[:10]:
                text = el.get_text(strip=True)[:150]
                if text:
                    extracted["headings"].append({"level": tag, "text": text})

        if soup.find("video") or soup.find("iframe", src=lambda s: s and ("youtube" in s or "vimeo" in s)):
            extracted["has_video"] = True
        if soup.find("form") or soup.find(attrs={"type": "email"}):
            extracted["has_form"] = True
        for kw in ["testimonial", "review", "quote"]:
            if soup.find(class_=lambda c: c and kw in str(c).lower()):
                extracted["has_testimonials"] = True
                break
        for kw in ["faq", "accordion", "question"]:
            if soup.find(class_=lambda c: c and kw in str(c).lower()):
                extracted["has_faq"] = True
                break
        for kw in ["pricing", "price", "plan", "tier"]:
            if soup.find(class_=lambda c: c and kw in str(c).lower()):
                extracted["has_pricing"] = True
                break
        for btn in soup.find_all(["button", "a"], class_=lambda c: c and any(k in str(c).lower() for k in ["btn", "button", "cta"])):
            text = btn.get_text(strip=True)[:50]
            if text and len(text) > 1:
                extracted["cta_texts"].append(text)
        extracted["cta_texts"] = extracted["cta_texts"][:5]

        # 3. Build AI prompt
        event_name = campaign.get('event_name', 'Mi Evento')
        event_date = campaign.get('event_date', 'Por confirmar')
        event_location = campaign.get('event_location', 'Por confirmar')
        event_speakers = campaign.get('event_speakers', '')
        event_description = campaign.get('event_description', '')

        clone_prompt = f"""Analiza la estructura de esta página web y genera una landing page que REPLIQUE su diseño, estilo visual y estructura de secciones, pero usando los datos de nuestra campaña.

═══ PÁGINA ORIGINAL ANALIZADA ═══
URL: {url}
Título: {extracted['title']}
Descripción: {extracted['description']}
Colores detectados: {', '.join(extracted['colors'][:8])}
Fuentes: {', '.join(extracted['fonts'][:4]) if extracted['fonts'] else 'No detectadas'}
Encabezados encontrados: {json.dumps(extracted['headings'][:8], ensure_ascii=False)}
CTAs encontrados: {', '.join(extracted['cta_texts'][:5])}
Tiene video: {extracted['has_video']}
Tiene formulario: {extracted['has_form']}
Tiene testimonios: {extracted['has_testimonials']}
Tiene FAQ: {extracted['has_faq']}
Tiene pricing: {extracted['has_pricing']}

═══ DATOS DE NUESTRA CAMPAÑA ═══
Nombre: {event_name}
Fecha: {event_date}
Lugar: {event_location}
Speakers: {event_speakers}
{f'Descripción: {event_description[:400]}' if event_description else ''}

Genera la landing page completa en el formato JSON estándar."""

        system_prompt = """You are an expert at cloning and replicating web page designs. Your job is to analyze the structure, colors, typography, and layout of a page and create a faithful replica using our JSON section system.

IMPORTANT RULES:
1. MATCH the original page's visual style: same color palette, same typography feel, same layout flow
2. REPLICATE the section structure: if the original has hero → benefits → testimonials → form, create the same flow
3. Use the DETECTED COLORS from the original page for the theme and section styles
4. Use the DETECTED FONTS or similar ones for font_heading and font_body
5. Adapt the CONTENT to our campaign data, but keep the DESIGN faithful to the original
6. Write compelling copy in the same language as the campaign data

Available section types:
hero: content={headline, subheadline, cta_text, cta_url, video_url, background_image}, style={background, text_color, cta_color}
countdown: content={headline, target_date (ISO)}, style={background, text_color}
benefits: content={headline, items:[{icon, title, description}]}, style={background, text_color}
speakers: content={headline, subheadline, layout, speakers:[{name, title, image_url, bio}]}, style={background, text_color}
testimonials: content={headline, items:[{name, text, image_url}]}, style={background, text_color}
video: content={headline, video_url, description}, style={background, text_color}
form: content={headline, subheadline, fields:["name","email","whatsapp"], cta_text, success_message}, style={background, text_color, cta_color}
faq: content={headline, items:[{question, answer}]}, style={background, text_color}
cta: content={headline, subheadline, cta_text, cta_url}, style={background, text_color, cta_color}
pricing: content={headline, subheadline, guarantee, tiers:[{name, price, description, features:[], cta_text, cta_url, highlighted:bool}]}, style={background, text_color, cta_color}
custom: content={html}, style={background, text_color}

Each section: {"id": "type-N", "type": "type", "order": N, "visible": true, "content": {...}, "style": {...}}
Return ONLY JSON: {"sections": [...], "theme": {primary_color, background_color, text_color, font_heading, font_body}}"""

        async with httpx.AsyncClient(timeout=120.0) as ai_client:
            if settings.anthropic_api_key:
                r = await ai_client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "claude-opus-4-20250514",
                        "max_tokens": 16384,
                        "temperature": 0.2,
                        "system": system_prompt,
                        "messages": [
                            {"role": "user", "content": clone_prompt},
                            {"role": "assistant", "content": "{"},
                        ],
                    }
                )
                if r.status_code != 200:
                    logger.error(f"Clone job {job_id}: Claude returned {r.status_code}: {r.text[:500]}")
                    raise Exception(f"Claude error: {r.status_code}")
                data = r.json()
                content = "{" + data["content"][0]["text"]
                logger.info(f"Clone job {job_id}: Claude response length={len(content)}")
            else:
                r = await ai_client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": clone_prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.2,
                        "max_tokens": 16384,
                    }
                )
                if r.status_code != 200:
                    logger.error(f"Clone job {job_id}: OpenAI returned {r.status_code}: {r.text[:500]}")
                    raise Exception(f"OpenAI error: {r.status_code}")
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                logger.info(f"Clone job {job_id}: OpenAI response length={len(content)}")

            # Parse JSON — strip markdown code blocks if present
            cleaned = content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].strip()
            result = json.loads(cleaned)
            result["cloned_from"] = {"url": url, "title": extracted["title"], "colors": extracted["colors"][:5], "fonts": extracted["fonts"][:3]}
            logger.info(f"Clone job {job_id}: Generated {len(result.get('sections', []))} sections")

        _JOBS[job_id] = {**_JOBS[job_id], "status": "done", "data": result}
        logger.info(f"Clone job {job_id}: DONE")
    except Exception as exc:
        logger.exception(f"Clone job {job_id} failed: {exc}")
        _JOBS[job_id] = {**_JOBS[job_id], "status": "error", "error": str(exc)[:300]}


@router.post("/landing-pages/clone-url")
async def clone_from_url(request: Request):
    """Clone a URL — returns job_id for polling."""
    body = await request.json()
    url = body.get("url", "").strip()
    campaign_id = body.get("campaign_id", "")

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if campaign_id:
        _validate_auth(request, campaign_id)

    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"status": "processing", "created_at": time.time()}

    logger.info(f"Starting clone job {job_id} for URL: {url}")
    asyncio.ensure_future(_run_clone_job(job_id, url, campaign_id))

    return {"ok": True, "job_id": job_id, "status": "processing"}


# ─── Signed Upload URL (browser uploads directly to Supabase) ────────────────

@router.get("/media/signed-upload-url")
async def get_signed_upload_url(
    request: Request,
    path: str = Query(...),
    bucket: str = Query("media"),
):
    """Generate a signed upload URL so the browser can upload directly to Supabase Storage.
    This bypasses Fly.io entirely — no size limits, no timeouts."""
    sb = _sb()
    try:
        result = sb.storage.from_(bucket).create_signed_upload_url(path)
        public_url = sb.storage.from_(bucket).get_public_url(path)
        return {
            "ok": True,
            "signed_url": result.get("signedUrl") or result.get("signed_url") or result.get("signedURL"),
            "token": result.get("token", ""),
            "path": result.get("path", path),
            "public_url": public_url,
        }
    except Exception as exc:
        logger.exception("signed_url_failed")
        raise HTTPException(status_code=500, detail=f"Failed: {str(exc)[:200]}")
