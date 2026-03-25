"""Landing page builder endpoints."""
import logging
import uuid
import json
import time
import random
import string
import asyncio
from datetime import datetime, timezone
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
        "name": "🔥 Evento Presencial",
        "category": "eventos",
        "description": "Landing premium para eventos presenciales de alto impacto con registro, speakers y urgencia",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "🔥 El Evento Que Va a Cambiar Las Reglas Del Juego", "subheadline": "Únete a cientos de emprendedores que van a descubrir el sistema exacto para escalar sus ventas en tiempo récord", "cta_text": "QUIERO MI LUGAR AHORA →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(160deg, #0f0c29 0%, #302b63 50%, #24243e 100%)", "text_color": "#ffffff", "cta_color": "#00d26a"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "¿Qué vas a descubrir en este evento?", "items": [{"icon": "💰", "title": "El Sistema de Ventas de 7 Cifras", "description": "El framework exacto que usan los negocios que facturan más de $100K al mes — paso a paso, sin teoría."}, {"icon": "🧠", "title": "Mentalidad de CEO, No de Empleado", "description": "Por qué el 95% se estanca y qué hacer diferente para estar en el 5% que escala."}, {"icon": "🤝", "title": "Networking Con Gente Que Juega en Grande", "description": "No vas a encontrar a estas personas en un webinar. Este es el tipo de conexión que cambia carreras."}, {"icon": "🚀", "title": "Tu Plan de Acción Para Los Próximos 90 Días", "description": "Sales del evento con un plan CLARO y ESPECÍFICO para tu negocio. Nada de inspiración vacía."}]}, "style": {"background": "#13111c", "text_color": "#ffffff"}},
            {"id": "countdown-1", "type": "countdown", "order": 3, "visible": True, "content": {"headline": "⏰ LOS LUGARES SE AGOTAN EN:", "target_date": ""}, "style": {"background": "linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Speakers de Clase Mundial", "subheadline": "Aprende directamente de quienes ya lograron lo que tú quieres lograr", "layout": "card", "speakers": [{"name": "Speaker Principal", "title": "CEO & Fundador", "image_url": "", "bio": "Ha construido un imperio de más de $10M en ventas online. Su método ha sido replicado por miles de emprendedores en 15 países."}]}, "style": {"background": "#0d0b1a", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 5, "visible": True, "content": {"headline": "Esto Es Lo Que Pasa Cuando Asistes", "items": [{"name": "Carolina M.", "text": "Llegué escéptica y salí con un plan tan claro que en 60 días tripliqué mis ventas. No exagero.", "image_url": ""}, {"name": "Roberto A.", "text": "El networking solo ya valió 10 veces lo que pagué. Cerré un deal de $25K con alguien que conocí ahí.", "image_url": ""}, {"name": "Diana L.", "text": "Llevo 3 años en eventos de negocios y NINGUNO se compara con este. La calidad del contenido es otro nivel.", "image_url": ""}]}, "style": {"background": "#13111c", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 6, "visible": True, "content": {"headline": "⚡ Precio Especial de Lanzamiento", "subheadline": "Este precio NO va a durar. Cada semana sube. Los primeros 50 en registrarse reciben acceso VIP a la sesión de networking exclusiva.", "cta_text": "ASEGURAR MI LUGAR →", "cta_url": "#form"}, "style": {"background": "linear-gradient(135deg, #302b63 0%, #24243e 100%)", "text_color": "#ffffff", "cta_color": "#00d26a"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "🎟️ Reserva Tu Lugar Ahora", "subheadline": "Cupo limitado — cuando se acaban, se acaban", "fields": ["name", "email", "whatsapp"], "cta_text": "RESERVAR MI LUGAR 🔥", "success_message": "¡LISTO! 🎉 Revisa tu WhatsApp — te acabamos de enviar todos los detalles."}, "style": {"background": "linear-gradient(160deg, #0f0c29 0%, #302b63 100%)", "text_color": "#ffffff", "cta_color": "#00d26a"}},
            {"id": "faq-1", "type": "faq", "order": 8, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Cuánto cuesta la entrada?", "answer": "La entrada general tiene un precio especial de lanzamiento. Regístrate ahora y te enviamos toda la info por WhatsApp."}, {"question": "¿Qué incluye el ticket VIP?", "answer": "Acceso a sesiones exclusivas, networking privado con speakers, cena VIP y material adicional que no se comparte en general."}, {"question": "¿Puedo ir con alguien?", "answer": "¡Claro! Trae a tu socio o a alguien de tu equipo. Tenemos precios especiales para equipos."}, {"question": "No puedo asistir, ¿habrá grabación?", "answer": "Este evento es PRESENCIAL y la magia está en vivirlo. No habrá grabación disponible. Es ahora o nunca."}]}, "style": {"background": "#0d0b1a", "text_color": "#ffffff"}}
        ],
        "theme": {"primary_color": "#00d26a", "secondary_color": "#7c3aed", "background_color": "#0f0c29", "text_color": "#ffffff", "font_heading": "Space Grotesk", "font_body": "DM Sans"}
    },
    "webinar": {
        "name": "🎥 Masterclass Online",
        "category": "eventos",
        "description": "Landing de alta conversión para webinars, masterclasses y clases en vivo online",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Masterclass GRATIS: El Método Que Está Cambiando Todo", "subheadline": "Descubre en 60 minutos el sistema exacto que emprendedores en 12 países usan para generar clientes todos los días — sin depender de suerte ni referidos", "cta_text": "RESERVAR MI LUGAR GRATIS →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)", "text_color": "#ffffff", "cta_color": "#fbbf24"}},
            {"id": "countdown-1", "type": "countdown", "order": 2, "visible": True, "content": {"headline": "🔴 EN VIVO — COMIENZA EN:", "target_date": ""}, "style": {"background": "#1e1b4b", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "En esta masterclass vas a aprender:", "items": [{"icon": "🎯", "title": "El Framework de Captación Automática", "description": "Cómo hacer que clientes potenciales lleguen a ti TODOS los días sin perseguir a nadie"}, {"icon": "💬", "title": "El Script de Ventas Que Cierra Solo", "description": "Las palabras exactas que convierten un 'me interesa' en un 'sí, quiero'"}, {"icon": "📊", "title": "La Fórmula de Escala Predecible", "description": "Cómo pasar de ingresos inconsistentes a un negocio que crece CADA mes"}, {"icon": "🤖", "title": "La Herramienta de AI Que Lo Cambia Todo", "description": "Lo que antes tomaba semanas ahora lo haces en minutos con inteligencia artificial"}]}, "style": {"background": "#0f0a2e", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Tu Instructor", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Fundador & CEO", "image_url": "", "bio": "Ha ayudado a más de 10,000 emprendedores a construir negocios digitales rentables. Su comunidad genera millones en ventas cada año."}]}, "style": {"background": "#1e1b4b", "text_color": "#ffffff"}},
            {"id": "benefits-2", "type": "benefits", "order": 5, "visible": True, "content": {"headline": "🎁 BONUS Exclusivo Por Asistir EN VIVO:", "items": [{"icon": "📋", "title": "Template del Funnel Completo", "description": "Copia y pega el embudo exacto que mostramos en la clase"}, {"icon": "🎥", "title": "Grabación de la Masterclass", "description": "Solo para quienes se registren — no estará disponible después"}, {"icon": "💰", "title": "Descuento Especial Solo Para Asistentes", "description": "Acceso a una oferta que NO encontrarás en ningún otro lado"}]}, "style": {"background": "#0f0a2e", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 6, "visible": True, "content": {"headline": "🔒 Reserva Tu Lugar — Es GRATIS", "subheadline": "Más de 500 personas ya se registraron. Cupo limitado a 1,000.", "fields": ["name", "email", "whatsapp"], "cta_text": "SÍ, QUIERO ASISTIR GRATIS 🎯", "success_message": "¡Registrado! 🎉 Te enviamos el link de acceso por WhatsApp. Llega 5 minutos antes."}, "style": {"background": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)", "text_color": "#ffffff", "cta_color": "#fbbf24"}}
        ],
        "theme": {"primary_color": "#fbbf24", "secondary_color": "#667eea", "background_color": "#0f0a2e", "text_color": "#ffffff", "font_heading": "Sora", "font_body": "Inter"}
    },
    "lead_magnet": {
        "name": "📘 Lead Magnet / Guía Gratis",
        "category": "captacion",
        "description": "Landing elegante para descargar ebooks, guías, checklists o recursos gratuitos",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "La Guía Que Hubiera Querido Tener Cuando Empecé", "subheadline": "Descarga GRATIS el playbook de 47 páginas con las estrategias exactas para lanzar un negocio digital rentable en 2026", "cta_text": "DESCARGAR MI GUÍA GRATIS →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #faf5ff 0%, #f0e7ff 50%, #e8dff5 100%)", "text_color": "#1a1a2e", "cta_color": "#7c3aed"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "Lo Que Vas a Encontrar Dentro:", "items": [{"icon": "📖", "title": "Cap. 1 — El Mapa del Negocio Digital", "description": "Entiende exactamente qué piezas necesitas y en qué orden armarlas para no perder tiempo"}, {"icon": "🎯", "title": "Cap. 2 — Cómo Elegir Tu Nicho Perfecto", "description": "La fórmula de 3 preguntas que te dice exactamente dónde está tu oportunidad de oro"}, {"icon": "💰", "title": "Cap. 3 — Tu Primera Venta en 7 Días", "description": "El método step-by-step para generar tu primer ingreso online esta semana"}, {"icon": "🚀", "title": "Cap. 4 — De $0 a $10K/mes", "description": "El roadmap realista con las acciones exactas semana por semana"}]}, "style": {"background": "#ffffff", "text_color": "#1a1a2e"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 3, "visible": True, "content": {"headline": "Miles de personas ya descargaron esta guía", "items": [{"name": "Alejandra V.", "text": "Leí la guía un viernes y el lunes ya estaba implementando. En 2 semanas hice mi primera venta online.", "image_url": ""}, {"name": "Miguel T.", "text": "He comprado cursos de $2,000 que no tienen ni la mitad del valor que tiene esta guía gratuita. Increíble.", "image_url": ""}]}, "style": {"background": "#faf5ff", "text_color": "#1a1a2e"}},
            {"id": "form-1", "type": "form", "order": 4, "visible": True, "content": {"headline": "📩 Descarga Tu Copia Gratuita", "subheadline": "Te la enviamos al instante por WhatsApp. Sin spam, sin trucos.", "fields": ["name", "email", "whatsapp"], "cta_text": "ENVIARME LA GUÍA AHORA 📘", "success_message": "¡Lista! 🎉 Revisa tu WhatsApp — tu guía ya está ahí."}, "style": {"background": "#ffffff", "text_color": "#1a1a2e", "cta_color": "#7c3aed"}}
        ],
        "theme": {"primary_color": "#7c3aed", "secondary_color": "#a78bfa", "background_color": "#faf5ff", "text_color": "#1a1a2e", "font_heading": "DM Serif Display", "font_body": "Plus Jakarta Sans"}
    },
    "vsl": {
        "name": "🎬 VSL (Video de Ventas)",
        "category": "ventas",
        "description": "Página de ventas con video, filtro de audiencia, múltiples CTAs y proceso de aplicación",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Tu Negocio No Tiene un Problema de Marketing.", "subheadline": "Tiene un problema de SISTEMA. Y en este video te voy a mostrar exactamente cómo resolverlo.", "cta_text": "VER EL VIDEO →", "cta_url": "#video", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(180deg, #0a0a0a 0%, #1a1a2e 100%)", "text_color": "#ffffff", "cta_color": "#ef4444"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "¿Te suena familiar?", "items": [{"icon": "😤", "title": "Inviertes en ads pero no recuperas la inversión", "description": ""}, {"icon": "😩", "title": "Generas leads pero no cierran", "description": ""}, {"icon": "😰", "title": "Tus ingresos son una montaña rusa", "description": ""}, {"icon": "😞", "title": "Trabajas 12 horas al día y no escalas", "description": ""}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 3, "visible": True, "content": {"headline": "El problema NO eres tú.", "subheadline": "Es que nadie te enseñó a construir un SISTEMA de ventas. No más tácticas sueltas. No más \"prueba esto y a ver\". En este video te muestro el framework completo.", "cta_text": "", "cta_url": ""}, "style": {"background": "#0a0a0a", "text_color": "#ffffff"}},
            {"id": "video-1", "type": "video", "order": 4, "visible": True, "content": {"headline": "👇 Mira Este Video Antes De Que Lo Quite", "video_url": "", "description": "", "video_autoplay": False, "video_controls": True, "video_muted": False, "video_loop": False}, "style": {"background": "#0a0a0a", "text_color": "#ffffff"}},
            {"id": "benefits-2", "type": "benefits", "order": 5, "visible": True, "content": {"headline": "Esto NO es para todos. Es para ti si:", "items": [{"icon": "✅", "title": "Ya tienes un negocio que genera ingresos", "description": ""}, {"icon": "✅", "title": "Estás dispuesto a invertir en escalar", "description": ""}, {"icon": "✅", "title": "Quieres un sistema, no otra táctica", "description": ""}, {"icon": "✅", "title": "Estás listo para jugar en grande", "description": ""}]}, "style": {"background": "#1a1a2e", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 6, "visible": True, "content": {"headline": "Personas Reales. Resultados Reales.", "items": [{"name": "Andrés F.", "text": "Pasé de $3K a $18K mensuales en 4 meses. El sistema funciona.", "image_url": ""}, {"name": "Laura S.", "text": "Por fin tengo predecibilidad en mi negocio. Sé exactamente cuánto voy a facturar cada mes.", "image_url": ""}, {"name": "Carlos R.", "text": "Recuperé mi inversión en la primera semana. No es exageración.", "image_url": ""}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "📞 Aplica Para Una Llamada de Diagnóstico", "subheadline": "Solo aceptamos 10 personas por semana. Si calificas, te contactamos en menos de 24 horas.", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO APLICAR AHORA →", "success_message": "¡Aplicación recibida! 📞 Te contactaremos por WhatsApp en las próximas 24 horas."}, "style": {"background": "linear-gradient(180deg, #1a1a2e 0%, #0a0a0a 100%)", "text_color": "#ffffff", "cta_color": "#ef4444"}}
        ],
        "theme": {"primary_color": "#ef4444", "secondary_color": "#f97316", "background_color": "#0a0a0a", "text_color": "#ffffff", "font_heading": "Outfit", "font_body": "Inter"}
    },
    "checkout_page": {
        "name": "💳 Página de Ventas",
        "category": "ventas",
        "description": "Página de ventas completa con pricing, testimonios, garantía y checkout",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "El Programa Que Ya Transformó a Miles de Emprendedores", "subheadline": "Accede al sistema paso a paso para construir un negocio digital que facture de forma predecible — mes tras mes", "cta_text": "VER LOS PLANES →", "cta_url": "#pricing", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #1e3a5f 0%, #0f1b2d 100%)", "text_color": "#ffffff", "cta_color": "#f59e0b"}},
            {"id": "video-1", "type": "video", "order": 2, "visible": True, "content": {"headline": "Mira Cómo Funciona el Programa", "video_url": "", "description": "", "video_autoplay": False, "video_controls": True, "video_muted": False, "video_loop": False}, "style": {"background": "#0f1b2d", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "Lo Que Incluye Tu Acceso:", "items": [{"icon": "🎓", "title": "8 Módulos en Video (40+ horas)", "description": "Todo el sistema organizado paso a paso. Sin relleno. Solo lo que funciona."}, {"icon": "📋", "title": "Templates y Scripts Listos Para Usar", "description": "Copia, pega, personaliza y lanza. Funnels, ads, emails, WhatsApp — todo incluido."}, {"icon": "🤝", "title": "Comunidad Privada de Emprendedores", "description": "Acceso a un grupo exclusivo donde compartimos resultados, estrategias y oportunidades."}, {"icon": "📞", "title": "Sesiones de Q&A en Vivo Semanales", "description": "Pregunta lo que necesites. Te respondemos en vivo cada semana."}, {"icon": "🤖", "title": "Herramientas de AI Incluidas", "description": "Acceso a tecnología que automatiza tu captación de clientes."}]}, "style": {"background": "#14253d", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 4, "visible": True, "content": {"headline": "💬 Resultados Reales de Nuestros Alumnos", "items": [{"name": "Santiago P.", "text": "De $0 a $8,500/mes en 90 días. El programa se pagó solo la primera semana. Lo recomiendo al 1000%.", "image_url": ""}, {"name": "Valentina R.", "text": "Dejé mi trabajo de 9 a 5 después del segundo mes. Ahora facturo más trabajando la mitad de horas.", "image_url": ""}, {"name": "Diego M.", "text": "Ya había probado otros programas. NINGUNO se compara. La diferencia es que este tiene un SISTEMA real, no solo motivación.", "image_url": ""}]}, "style": {"background": "#0f1b2d", "text_color": "#ffffff"}},
            {"id": "pricing-1", "type": "pricing", "order": 5, "visible": True, "content": {"headline": "Elige Tu Plan", "subheadline": "Todos los planes incluyen acceso al programa completo + comunidad + herramientas AI", "guarantee": "30 días de garantía total. Si no ves valor, te devolvemos tu dinero. Sin preguntas.", "tiers": [{"name": "Esencial", "price": "$497 USD", "description": "Todo lo que necesitas para empezar", "features": ["8 módulos completos", "Templates y scripts", "Comunidad privada", "Acceso de por vida"], "cta_text": "ELEGIR ESENCIAL", "cta_url": "#form", "highlighted": False}, {"name": "Premium", "price": "$997 USD", "description": "Lo más popular — el mejor valor", "features": ["Todo lo de Esencial", "Sesiones de Q&A en vivo", "Herramientas AI incluidas", "1 mentoría 1-a-1", "Soporte prioritario"], "cta_text": "ELEGIR PREMIUM ⭐", "cta_url": "#form", "highlighted": True}, {"name": "VIP", "price": "$2,497 USD", "description": "Para quien quiere resultados rápidos", "features": ["Todo lo de Premium", "4 mentorías 1-a-1 mensuales", "Revisión de tu negocio", "Acceso al inner circle", "Soporte directo por WhatsApp"], "cta_text": "ELEGIR VIP", "cta_url": "#form", "highlighted": False}]}, "style": {"background": "#14253d", "text_color": "#ffffff", "cta_color": "#f59e0b"}},
            {"id": "faq-1", "type": "faq", "order": 6, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Hay garantía de devolución?", "answer": "Sí, 30 días de garantía total. Si el programa no cumple tus expectativas, te devolvemos cada centavo."}, {"question": "¿Cuánto tiempo tengo acceso?", "answer": "Acceso de por vida. Incluyendo todas las actualizaciones futuras."}, {"question": "¿Puedo pagar en cuotas?", "answer": "Sí, ofrecemos planes de 3 y 6 cuotas. Te lo explicamos por WhatsApp al registrarte."}, {"question": "¿Funciona para mi tipo de negocio?", "answer": "Si vendes productos o servicios (físicos o digitales) y quieres más clientes, sí. El sistema es adaptable a cualquier industria."}]}, "style": {"background": "#0f1b2d", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "🚀 Empieza Hoy Mismo", "subheadline": "Regístrate y te contactamos por WhatsApp para activar tu acceso", "fields": ["name", "email", "whatsapp"], "cta_text": "QUIERO EMPEZAR AHORA 🔥", "success_message": "¡Excelente decisión! 🎉 Te contactamos por WhatsApp en minutos para activar tu acceso."}, "style": {"background": "linear-gradient(135deg, #1e3a5f 0%, #0f1b2d 100%)", "text_color": "#ffffff", "cta_color": "#f59e0b"}}
        ],
        "theme": {"primary_color": "#f59e0b", "secondary_color": "#3b82f6", "background_color": "#0f1b2d", "text_color": "#ffffff", "font_heading": "Space Grotesk", "font_body": "DM Sans"}
    },
    "marca_personal": {
        "name": "👤 Marca Personal / Empresa",
        "category": "branding",
        "description": "Sitio web profesional para tu marca personal o empresa — como spencerhoffmann.com",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Tu Nombre Aquí", "subheadline": "Emprendedor · Speaker · Mentor — Ayudo a personas ambiciosas a construir negocios digitales que generen libertad financiera y de tiempo", "cta_text": "CONOCE MI HISTORIA →", "cta_url": "#about", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #0c0c0c 0%, #1a1a1a 50%, #0c0c0c 100%)", "text_color": "#ffffff", "cta_color": "#c9a84c"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "Lo Que Hago", "items": [{"icon": "🎤", "title": "Speaker Internacional", "description": "He compartido escenario con los referentes más importantes de la industria digital en más de 20 países."}, {"icon": "🎓", "title": "Mentor de Emprendedores", "description": "Mi comunidad de más de 40,000 emprendedores genera millones en ventas cada año con mis métodos."}, {"icon": "💼", "title": "Fundador & CEO", "description": "Construí desde cero una empresa de tecnología que hoy sirve a miles de clientes en toda Latinoamérica."}, {"icon": "📱", "title": "Creador de Contenido", "description": "Más de 5 millones de personas siguen mi contenido sobre negocios, mentalidad y crecimiento personal."}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 3, "visible": True, "content": {"headline": "Sobre Mí", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Fundador & CEO", "image_url": "", "bio": "Desde muy joven supe que no quería una vida \"normal\". Empecé mi primer negocio a los 19 años, fracasé muchas veces, y hoy ayudo a miles de personas a construir la vida que quieren a través de negocios digitales. Mi misión es democratizar el acceso a herramientas y conocimiento para que cualquier persona pueda emprender sin importar de dónde venga."}]}, "style": {"background": "#0c0c0c", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 4, "visible": True, "content": {"headline": "Lo Que Dicen De Mi Trabajo", "items": [{"name": "Empresario exitoso", "text": "Es una de las personas más brillantes que conozco en el mundo digital. Su capacidad de simplificar lo complejo es extraordinaria.", "image_url": ""}, {"name": "Alumna destacada", "text": "Gracias a su mentoría pasé de tener una idea a tener un negocio real que hoy factura 5 cifras al mes.", "image_url": ""}, {"name": "Colega de industria", "text": "Lo que más admiro es su autenticidad. En una industria llena de humo, él entrega resultados reales.", "image_url": ""}]}, "style": {"background": "#111111", "text_color": "#ffffff"}},
            {"id": "video-1", "type": "video", "order": 5, "visible": True, "content": {"headline": "🎥 Conoce Mi Visión", "video_url": "", "description": "", "video_autoplay": False, "video_controls": True, "video_muted": False, "video_loop": False}, "style": {"background": "#0c0c0c", "text_color": "#ffffff"}},
            {"id": "cta-1", "type": "cta", "order": 6, "visible": True, "content": {"headline": "¿Quieres Trabajar Conmigo?", "subheadline": "Ya sea que quieras asistir a uno de mis eventos, unirte a mi mentoría, o simplemente conectar — el primer paso es este:", "cta_text": "CONECTA CONMIGO →", "cta_url": "#form"}, "style": {"background": "linear-gradient(135deg, #1a1a1a 0%, #0c0c0c 100%)", "text_color": "#ffffff", "cta_color": "#c9a84c"}},
            {"id": "form-1", "type": "form", "order": 7, "visible": True, "content": {"headline": "📩 Hablemos", "subheadline": "Déjame tus datos y mi equipo te contactará", "fields": ["name", "email", "whatsapp"], "cta_text": "ENVIAR MENSAJE →", "success_message": "¡Mensaje recibido! Te contactaremos pronto por WhatsApp."}, "style": {"background": "#0c0c0c", "text_color": "#ffffff", "cta_color": "#c9a84c"}}
        ],
        "theme": {"primary_color": "#c9a84c", "secondary_color": "#d4af37", "background_color": "#0c0c0c", "text_color": "#ffffff", "font_heading": "Playfair Display", "font_body": "Inter"}
    },
    "curso_online": {
        "name": "🎓 Curso / Programa Online",
        "category": "ventas",
        "description": "Landing para vender cursos, programas online o membresías con módulos y pricing",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Domina Tu Habilidad. Transforma Tu Ingreso.", "subheadline": "El programa online de 8 semanas que te da el sistema completo para construir un negocio digital rentable — desde cero hasta tus primeros $10K", "cta_text": "QUIERO INSCRIBIRME →", "cta_url": "#pricing", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #4f46e5 0%, #7c3aed 50%, #9333ea 100%)", "text_color": "#ffffff", "cta_color": "#22c55e"}},
            {"id": "video-1", "type": "video", "order": 2, "visible": True, "content": {"headline": "Mira Lo Que Vas a Aprender", "video_url": "", "description": "", "video_autoplay": False, "video_controls": True, "video_muted": False, "video_loop": False}, "style": {"background": "#1e1b4b", "text_color": "#ffffff"}},
            {"id": "benefits-1", "type": "benefits", "order": 3, "visible": True, "content": {"headline": "📚 Contenido del Programa", "items": [{"icon": "1️⃣", "title": "Semanas 1-2: Los Fundamentos", "description": "Construye las bases de tu negocio digital. Define tu nicho, tu oferta irresistible y tu cliente ideal."}, {"icon": "2️⃣", "title": "Semanas 3-4: Tu Sistema de Captación", "description": "Crea tu funnel completo: landing page + ads + WhatsApp automation. Todo conectado y funcionando."}, {"icon": "3️⃣", "title": "Semanas 5-6: Ventas Que Cierran Solas", "description": "Implementa el proceso de ventas que convierte leads en clientes sin que tú tengas que estar presente."}, {"icon": "4️⃣", "title": "Semanas 7-8: Escala y Automatiza", "description": "Pon todo en piloto automático. Ads que escalan, AI que trabaja por ti, y un negocio que crece solo."}]}, "style": {"background": "#0f0a2e", "text_color": "#ffffff"}},
            {"id": "speakers-1", "type": "speakers", "order": 4, "visible": True, "content": {"headline": "Tu Instructor", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Fundador & Mentor", "image_url": "", "bio": "Ha ayudado a más de 10,000 emprendedores a lanzar negocios digitales rentables. Su método ha generado millones en ventas en más de 15 países."}]}, "style": {"background": "#1e1b4b", "text_color": "#ffffff"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 5, "visible": True, "content": {"headline": "📊 Historias de Éxito", "items": [{"name": "Fernanda G.", "text": "Empecé sin saber nada. En 8 semanas tenía mi primer cliente y al tercer mes ya facturaba $5K/mes. El programa funciona.", "image_url": ""}, {"name": "Martín L.", "text": "La mejor inversión que he hecho. No es teoría — es un sistema que copias, implementas y funciona.", "image_url": ""}, {"name": "Sofía H.", "text": "Dejé de dar vueltas. En la primera semana ya tenía mi funnel listo y corriendo ads. Hoy facturo 5 cifras.", "image_url": ""}]}, "style": {"background": "#0f0a2e", "text_color": "#ffffff"}},
            {"id": "pricing-1", "type": "pricing", "order": 6, "visible": True, "content": {"headline": "Elige Tu Nivel", "subheadline": "Invierte en ti hoy. Los resultados empiezan esta semana.", "guarantee": "Garantía de 30 días. Si no ves valor, te devolvemos tu dinero. Cero riesgo.", "tiers": [{"name": "Self-Study", "price": "$297 USD", "description": "Aprende a tu ritmo", "features": ["8 módulos completos", "Templates y scripts", "Acceso de por vida", "Comunidad privada"], "cta_text": "ELEGIR SELF-STUDY", "cta_url": "#form", "highlighted": False}, {"name": "Con Mentoría", "price": "$697 USD", "description": "La opción más popular", "features": ["Todo lo de Self-Study", "Q&A en vivo semanal", "Revisión de tu negocio", "Soporte prioritario"], "cta_text": "ELEGIR CON MENTORÍA ⭐", "cta_url": "#form", "highlighted": True}]}, "style": {"background": "#1e1b4b", "text_color": "#ffffff", "cta_color": "#22c55e"}},
            {"id": "faq-1", "type": "faq", "order": 7, "visible": True, "content": {"headline": "Preguntas Frecuentes", "items": [{"question": "¿Necesito experiencia previa?", "answer": "No. El programa está diseñado desde cero. Si sabes usar internet, puedes hacer esto."}, {"question": "¿Cuánto tiempo necesito dedicarle?", "answer": "5-10 horas por semana es ideal. Pero todo queda grabado — puedes avanzar a tu ritmo."}, {"question": "¿Qué pasa si no funciona para mí?", "answer": "Tienes 30 días de garantía. Si el programa no cumple tus expectativas, te devolvemos tu dinero completo."}]}, "style": {"background": "#0f0a2e", "text_color": "#ffffff"}},
            {"id": "form-1", "type": "form", "order": 8, "visible": True, "content": {"headline": "🚀 Inscríbete Ahora", "subheadline": "Precio de lanzamiento disponible por tiempo limitado", "fields": ["name", "email", "whatsapp"], "cta_text": "COMENZAR MI TRANSFORMACIÓN →", "success_message": "¡Inscrito! 🎉 Te contactamos por WhatsApp para activar tu acceso."}, "style": {"background": "linear-gradient(135deg, #4f46e5 0%, #9333ea 100%)", "text_color": "#ffffff", "cta_color": "#22c55e"}}
        ],
        "theme": {"primary_color": "#22c55e", "secondary_color": "#7c3aed", "background_color": "#0f0a2e", "text_color": "#ffffff", "font_heading": "Sora", "font_body": "DM Sans"}
    },
    "agendar_cita": {
        "name": "📅 Agendar Cita / Servicio",
        "category": "servicios",
        "description": "Landing profesional para agendar llamadas de diagnóstico, consultas o servicios",
        "sections": [
            {"id": "hero-1", "type": "hero", "order": 1, "visible": True, "content": {"headline": "Tu Negocio Merece Una Estrategia Real", "subheadline": "Agenda una sesión de diagnóstico GRATUITA de 30 minutos donde vamos a analizar tu situación, identificar exactamente qué te frena, y diseñar un plan de acción personalizado", "cta_text": "AGENDAR MI SESIÓN GRATIS →", "cta_url": "#form", "video_url": "", "background_image": ""}, "style": {"background": "linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 50%, #bae6fd 100%)", "text_color": "#0c4a6e", "cta_color": "#0284c7"}},
            {"id": "benefits-1", "type": "benefits", "order": 2, "visible": True, "content": {"headline": "¿Cómo Funciona?", "items": [{"icon": "1️⃣", "title": "Agenda Tu Sesión", "description": "Elige el horario que te funcione mejor. Nos adaptamos a tu disponibilidad."}, {"icon": "2️⃣", "title": "Diagnóstico de Tu Negocio", "description": "En 30 minutos analizamos dónde estás, a dónde quieres llegar, y qué te está frenando."}, {"icon": "3️⃣", "title": "Tu Plan de Acción", "description": "Sales de la llamada con un plan claro, específico y accionable para los próximos 30 días."}]}, "style": {"background": "#ffffff", "text_color": "#0c4a6e"}},
            {"id": "speakers-1", "type": "speakers", "order": 3, "visible": True, "content": {"headline": "¿Con Quién Vas a Hablar?", "layout": "horizontal", "speakers": [{"name": "Tu Nombre", "title": "Consultor Estratégico", "image_url": "", "bio": "Especialista en crecimiento de negocios digitales. Ha ayudado a cientos de emprendedores a duplicar y triplicar sus ingresos con sistemas probados."}]}, "style": {"background": "#f0f9ff", "text_color": "#0c4a6e"}},
            {"id": "testimonials-1", "type": "testimonials", "order": 4, "visible": True, "content": {"headline": "Lo Que Dicen Quienes Ya Tomaron Su Sesión", "items": [{"name": "Patricia V.", "text": "En 30 minutos me dio más claridad que en 6 meses tratando de resolverlo sola. Implementé su plan y dupliqué ventas.", "image_url": ""}, {"name": "Eduardo S.", "text": "Pensé que iba a ser una llamada de ventas. Me equivoqué. Fue la conversación más productiva que tuve en todo el año.", "image_url": ""}]}, "style": {"background": "#ffffff", "text_color": "#0c4a6e"}},
            {"id": "cta-1", "type": "cta", "order": 5, "visible": True, "content": {"headline": "⚠️ Importante: Esto NO Es Para Todos", "subheadline": "Solo trabajamos con emprendedores que ya tienen un negocio activo y están listos para escalar. Si apenas estás empezando, esta sesión no es para ti — todavía.", "cta_text": "SÍ, QUIERO MI SESIÓN →", "cta_url": "#form"}, "style": {"background": "#f0f9ff", "text_color": "#0c4a6e", "cta_color": "#0284c7"}},
            {"id": "form-1", "type": "form", "order": 6, "visible": True, "content": {"headline": "📅 Agenda Tu Sesión Ahora", "subheadline": "Solo 5 sesiones disponibles esta semana. Se asignan por orden de registro.", "fields": ["name", "email", "whatsapp"], "cta_text": "AGENDAR MI SESIÓN GRATIS →", "success_message": "¡Agendado! 📞 Te contactaremos por WhatsApp para confirmar tu horario."}, "style": {"background": "#ffffff", "text_color": "#0c4a6e", "cta_color": "#0284c7"}}
        ],
        "theme": {"primary_color": "#0284c7", "secondary_color": "#0ea5e9", "background_color": "#f0f9ff", "text_color": "#0c4a6e", "font_heading": "Outfit", "font_body": "Plus Jakarta Sans"}
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


# ─── Templates List (MUST be before {page_id} route) ──────────────────────

@router.get("/landing-pages/templates")
async def list_templates():
    """List available landing page templates."""
    return {
        "ok": True,
        "data": [
            {"id": k, "name": v["name"], "description": v["description"],
             "category": v.get("category", "general"), "section_count": len(v["sections"])}
            for k, v in TEMPLATES.items()
        ]
    }


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
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

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


# Templates list endpoint moved above {page_id} route to avoid conflict


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
            # If content is a list of content blocks (text + images), extract text only
            # This avoids sending empty/broken base64 images to the API
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        # Skip image blocks — they bloat context and can have empty base64
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts).strip()
                if not content:
                    continue
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
                # Strip image blocks from content arrays (same as Claude path)
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    content = "\n".join(text_parts).strip()
                    if not content:
                        continue
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
    # SSRF protection: only allow http/https and public hostnames
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs allowed")
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or hostname.startswith("10.") or hostname.startswith("192.168.") or hostname.startswith("172."):
        raise HTTPException(status_code=400, detail="Internal URLs not allowed")
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
