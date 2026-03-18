from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import struct
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import websockets

from ..settings import settings

logger = logging.getLogger("ai_voice")

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
ELEVENLABS_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
    "?model_id={model_id}&output_format=ulaw_8000"
)


# ─── Audio resampling helpers ────────────────────────────────────────────────


def _resample_8k_to_24k(pcm16_8k: bytes) -> bytes:
    """Simple 3x linear interpolation for 8kHz -> 24kHz.

    Each sample is a signed 16-bit integer (2 bytes, little-endian).
    For each pair of consecutive samples, we output the first sample
    then two linearly interpolated samples between them.
    """
    if len(pcm16_8k) < 2:
        return b""

    num_samples = len(pcm16_8k) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm16_8k[: num_samples * 2])

    out: list[int] = []
    for i in range(num_samples - 1):
        s0 = samples[i]
        s1 = samples[i + 1]
        out.append(s0)
        out.append(s0 + (s1 - s0) // 3)
        out.append(s0 + 2 * (s1 - s0) // 3)

    # Last sample: repeat 3x (no next sample to interpolate toward)
    if num_samples > 0:
        out.append(samples[-1])
        out.append(samples[-1])
        out.append(samples[-1])

    return struct.pack(f"<{len(out)}h", *out)


def _resample_24k_to_8k(pcm16_24k: bytes) -> bytes:
    """Simple 3x decimation for 24kHz -> 8kHz.

    Take every 3rd sample.
    """
    num_samples = len(pcm16_24k) // 2
    if num_samples == 0:
        return b""

    samples = struct.unpack(f"<{num_samples}h", pcm16_24k[: num_samples * 2])

    out: list[int] = [samples[i] for i in range(0, num_samples, 3)]
    return struct.pack(f"<{len(out)}h", *out)


def _format_date_for_voice(iso_str: str) -> str:
    """Convert ISO date string to a human-readable date for voice prompts.

    '2026-03-27T15:00:00+00:00' -> '27 de marzo de 2026'
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        months_es = {
            1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
            5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
            9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
        }
        return f"{dt.day} de {months_es[dt.month]} de {dt.year}"
    except Exception:
        return iso_str


# ─── System prompt builder ───────────────────────────────────────────────────


def _get_purpose_template(purpose: str) -> dict[str, str]:
    """Return objective, flow, and rules for a given call purpose."""
    templates: dict[str, dict[str, str]] = {
        "confirm_attendance": {
            "objective": (
                "Confirmar la asistencia de {lead_name} a {event_name}, "
                "verificar que ya tiene su boleto general, "
                "edificar el evento para que se emocione, y si muestra interés "
                "ofrecerle la experiencia vi-ai-pi."
            ),
            "flow": (
                "1. APERTURA: Saluda a {lead_name} por su nombre. Identifícate. "
                "Di que le llamas porque viste que se registró a {event_name}. PARA y ESPERA respuesta.\n"
                "2. BOLETO GENERAL: Pregunta si ya pudo generar su boleto general de acceso (sin costo). "
                "Si NO lo tiene, dile que lo puede obtener respondiendo el mensaje de WhatsApp con su nombre y correo. "
                "PARA y ESPERA respuesta.\n"
                "3. CONTEXTO: Confirma que sí tiene pensado asistir. PARA y ESPERA respuesta.\n"
                "4. EDIFICACIÓN: Cuéntale brevemente de qué trata el evento y por qué es importante que asista. "
                "Usa la info de la transformación del evento. PARA y ESPERA respuesta.\n"
                "5. INTENCIÓN: Dile que tu intención es ayudarle a que asista y viva una gran experiencia. "
                "Pregunta si hay algo que necesite saber. PARA y ESPERA respuesta.\n"
                "6. VI-AI-PI: Si el momento es natural, menciona que ya le debió de haber llegado "
                "información de la experiencia vi-ai-pi a su teléfono. Si muestra interés, USA send_vip_whatsapp. "
                "PARA y ESPERA respuesta.\n"
                "7. LOGÍSTICA: Comparte fecha, lugar, hora de llegada si los tienes. PARA y ESPERA.\n"
                "8. CIERRE: Refuerza que no se lo puede perder. Dale un CTA claro: "
                "'Revisa tu WhatsApp que ahí te mandamos toda la información' o "
                "'Te mando tu link para asegurar tu lugar vi-ai-pi'. "
                "Despídete con energía y usa la función end_call."
            ),
            "rules": (
                "- NO saltes pasos. Sigue el flujo en orden.\n"
                "- Si la persona dice que no puede ir, empatiza y pregunta por qué.\n"
                "- Si muestra interés en vi-ai-pi, USA send_vip_whatsapp para enviarle la info.\n"
                "- Recuerda que la info de VIP ya le debió de haber llegado por WhatsApp.\n"
                "- El cierre SIEMPRE debe tener un llamado a la acción concreto.\n"
                "- Cuando termines de despedirte, SIEMPRE usa la función end_call."
            ),
        },
        "complete_registration": {
            "objective": (
                "Ayudar a {lead_name} a completar su registro para {event_name}. "
                "Esta persona AÚN NO tiene su boleto general. "
                "PRIMERO ayúdale a registrarse. DESPUÉS, si tiene su boleto, ofrece vi-ai-pi."
            ),
            "flow": (
                "1. APERTURA: Saluda a {lead_name}. Identifícate. "
                "Menciona que viste que empezó su registro en {event_name} pero parece que no lo completó. "
                "PARA y ESPERA respuesta.\n"
                "2. REGISTRO: Pregunta si necesita ayuda para completar su registro. "
                "Explica que para obtener su boleto general (sin costo) solo necesita enviarnos su nombre "
                "y correo por WhatsApp. PARA y ESPERA respuesta.\n"
                "3. DATOS: Si te da su nombre y correo durante la llamada, tómalos mentalmente. "
                "Dile que le vas a mandar un mensaje por WhatsApp para confirmar su registro. "
                "PARA y ESPERA respuesta.\n"
                "4. EDIFICACIÓN: Una vez que tenga claro cómo registrarse, cuéntale brevemente "
                "sobre el evento para emocionarlo. Usa la transformación del evento. PARA y ESPERA.\n"
                "5. VI-AI-PI: Si el momento es natural y ya tiene claro su registro general, "
                "menciona la experiencia vi-ai-pi. Si muestra interés, USA send_vip_whatsapp. "
                "PARA y ESPERA.\n"
                "6. CIERRE: Recuérdale que complete su registro por WhatsApp. "
                "Dile: 'Revisa tu teléfono, ahí te mandamos la información'. "
                "Despídete con energía y usa end_call."
            ),
            "rules": (
                "- PRIORIDAD #1: que complete su registro y obtenga su boleto general.\n"
                "- NO saltes a vi-ai-pi sin antes haber abordado el registro general.\n"
                "- Si ya tiene su boleto general, cambia el flujo hacia vi-ai-pi.\n"
                "- Cuando termines de despedirte, SIEMPRE usa la función end_call."
            ),
        },
        "sell_vip": {
            "objective": (
                "Ofrecer la experiencia vi-ai-pi a {lead_name} para {event_name}. "
                "Precio vi-ai-pi: {vip_price}."
            ),
            "flow": (
                "1. APERTURA: Saluda a {lead_name}. Identifícate. PARA y ESPERA respuesta.\n"
                "2. CONTEXTO: Menciona que viste su registro y quieres contarle algo especial. PARA y ESPERA.\n"
                "3. EDIFICACIÓN: Cuéntale de la transformación del evento. PARA y ESPERA.\n"
                "4. OFERTA: Presenta la experiencia vi-ai-pi con sus beneficios. PARA y ESPERA.\n"
                "5. CIERRE: Si muestra interés, USA send_vip_whatsapp. Da un CTA claro. "
                "Si tiene dudas, resuelve con empatía. Despídete y usa end_call."
            ),
            "rules": (
                "- Sé persuasiva pero no agresiva. Enfócate en el VALOR.\n"
                "- Si dice que no, respeta su decisión y confirma asistencia general.\n"
                "- Cuando termines de despedirte, SIEMPRE usa la función end_call."
            ),
        },
        "post_event_thanks": {
            "objective": (
                "Agradecer a {lead_name} por asistir a {event_name} y obtener feedback."
            ),
            "flow": (
                "1. Saluda y agradece su asistencia. PARA y ESPERA respuesta.\n"
                "2. Pregunta cómo fue su experiencia. PARA y ESPERA.\n"
                "3. Pregunta qué fue lo que más le gustó. PARA y ESPERA.\n"
                "4. Pregunta si hay algo que mejorarían. PARA y ESPERA.\n"
                "5. Agradece, despídete y usa end_call."
            ),
            "rules": (
                "- Sé genuinamente agradecida.\n"
                "- Escucha más de lo que hablas.\n"
                "- NO intentes vender nada en esta llamada.\n"
                "- Cuando termines, SIEMPRE usa la función end_call."
            ),
        },
        "no_show_follow_up": {
            "objective": (
                "Contactar a {lead_name} que no asistió a {event_name}, mantener la relación."
            ),
            "flow": (
                "1. Saluda amablemente. PARA y ESPERA respuesta.\n"
                "2. Menciona que lo extrañaron en el evento. PARA y ESPERA.\n"
                "3. Pregunta si todo está bien. PARA y ESPERA.\n"
                "4. Si hay interés, menciona futuros eventos. PARA y ESPERA.\n"
                "5. Despídete con calidez y usa end_call."
            ),
            "rules": (
                "- NO hagas sentir culpable al lead.\n"
                "- Sé empática y comprensiva.\n"
                "- Cuando termines, SIEMPRE usa la función end_call."
            ),
        },
        "payment_reminder": {
            "objective": (
                "Recordar a {lead_name} su pago pendiente para {event_name}."
            ),
            "flow": (
                "1. Saluda y confirma identidad. PARA y ESPERA respuesta.\n"
                "2. Menciona que su pago vi-ai-pi está pendiente. PARA y ESPERA.\n"
                "3. Pregunta si tuvo problemas con el pago. PARA y ESPERA.\n"
                "4. Ofrece reenviar link por WhatsApp. PARA y ESPERA.\n"
                "5. Despídete y usa end_call."
            ),
            "rules": (
                "- Sé amable, NO cobradora.\n"
                "- Si dice que ya no quiere vi-ai-pi, respeta su decisión.\n"
                "- Cuando termines, SIEMPRE usa la función end_call."
            ),
        },
        "survey": {
            "objective": (
                "Realizar encuesta breve a {lead_name} sobre {event_name}."
            ),
            "flow": (
                "1. Saluda y explica que será breve (2-3 min). PARA y ESPERA.\n"
                "2. Del 1 al 10: ¿Cómo califica el evento? PARA y ESPERA.\n"
                "3. ¿Qué fue lo mejor? PARA y ESPERA.\n"
                "4. ¿Qué mejoraría? PARA y ESPERA.\n"
                "5. ¿Asistiría a un próximo evento? PARA y ESPERA.\n"
                "6. Agradece y usa end_call."
            ),
            "rules": (
                "- Mantén la encuesta breve.\n"
                "- NO desvíes a ventas.\n"
                "- Cuando termines, SIEMPRE usa la función end_call."
            ),
        },
    }

    return templates.get(purpose, templates["confirm_attendance"])


def build_voice_system_prompt(
    *,
    campaign: dict,
    lead: dict,
    event_facts: dict,
    purpose: str = "confirm_attendance",
    use_elevenlabs: bool = False,
) -> str:
    """Build system prompt for AI voice calls.

    Uses campaign.ai_voice_system_prompt if available (and purpose is custom),
    otherwise generates a purpose-specific prompt from templates.
    """
    # Custom prompt: use campaign's own system prompt verbatim
    if purpose == "custom":
        custom = (campaign.get("ai_voice_system_prompt") or "").strip()
        if custom:
            return custom

    character_name = (campaign.get("ai_character_name") or "").strip() or "Ana"
    event_name = event_facts.get("event_name", "el evento")
    event_start = event_facts.get("starts_at", "") or event_facts.get("event_date", "")
    event_end = event_facts.get("ends_at", "")
    event_place = event_facts.get("address", "") or event_facts.get("event_place", "")
    speakers = event_facts.get("speakers", "") or event_facts.get("event_speakers", "")
    vip_price = event_facts.get("vip_price_usd", "") or event_facts.get("vip_price", "")
    lead_name = lead.get("name", "") or "amigo"
    transformation = event_facts.get("transformation", "")
    arrival_time = event_facts.get("arrival_time", "")
    lead_city = lead.get("city", "") or lead.get("country", "")
    lead_status = (lead.get("status") or "").upper().strip()
    whatsapp_from = (campaign.get("twilio_whatsapp_from") or "").strip()
    whatsapp_display = whatsapp_from.replace("whatsapp:", "").strip()

    # Get purpose-specific template
    tmpl = _get_purpose_template(purpose)

    # Format placeholders
    fmt = {
        "lead_name": lead_name,
        "event_name": event_name,
        "vip_price": vip_price or "precio especial",
    }
    objective = tmpl["objective"].format(**fmt)
    flow = tmpl["flow"].format(**fmt)
    rules = tmpl["rules"].format(**fmt)

    # Build AI identity line
    ai_identity = campaign.get("ai_identity") or ""
    if not ai_identity:
        if use_elevenlabs:
            # When using the organizer's cloned voice
            ai_identity = (
                f"Eres la inteligencia artificial de Spencer Hoffmann. "
                "Hablas con la voz de Spencer. Cuando te presentes di: "
                "'Soy la inteligencia artificial de Spencer Hoffmann del equipo de Beyond Wealth'. "
                "Si te preguntan quién eres, repite que eres la IA de Spencer."
            )
        else:
            ai_identity = (
                f"Eres {character_name}, una inteligencia artificial del equipo organizador. "
                "Cuando te presentes, menciona que eres una IA del equipo."
            )

    lines = [
        f"{ai_identity} Estás hablando por teléfono.",
        "",
        "═══ REGLA #1: NO HABLES DE CORRIDO ═══",
        "Esta es la regla MÁS IMPORTANTE de toda la llamada:",
        "- Después de CADA idea o pregunta: PARA. CÁLLATE. ESPERA a que la persona responda.",
        "- NUNCA encadenes dos preguntas seguidas.",
        "- NUNCA digas una frase y luego agregues otra sin haber escuchado respuesta.",
        "- Si dijiste algo, PARA. Aunque haya silencio, ESPERA.",
        "- Máximo 2 oraciones por turno. Luego SILENCIO.",
        "- Piensa en esto: si fueras la persona, ¿te gustaría que te hablaran sin parar? NO.",
        "- Si violas esta regla, la conversación se arruina.",
        "",
        "═══ ESTILO DE VOZ ═══",
        "- Habla como una PERSONA REAL, no como texto leído.",
        "- Usa muletillas naturales: 'oye', 'mira', 'fíjate', 'la verdad es que'.",
        "- NO uses emojis, markdown, asteriscos ni formato especial.",
        "- Sé cálida, entusiasta y genuina. Que se sienta la emoción por el evento.",
        "- Si no entiendes algo, di '¿me puedes repetir eso?'.",
        "",
        "═══ PRONUNCIACIÓN DE TÉRMINOS CLAVE ═══",
        "Estas palabras son en inglés. Pronúncialas CORRECTAMENTE:",
        "- VIP → pronúncialo 'vi-ai-pi' (tres sílabas separadas, NO 'bip' ni 'biaip')",
        "- Spencer Hoffmann → 'Spencer Jofman'",
        "- Beyond Wealth → 'Biond Welz'",
        "- networking → 'net-working'",
        "- QR → 'quiu-ar'",
        "- check-in → 'chek-in'",
        "- Zoom → 'zum'",
        "- Diamond → 'daimond'",
        "IMPORTANTE: Cada vez que vayas a decir VIP, recuerda: son tres letras separadas vi-ai-pi.",
        "",
        "═══ PERSONALIZACIÓN ═══",
        f"- Usa el nombre '{lead_name}' durante la conversación (al menos 3 veces).",
        "- Hazlo sentir especial: 'qué bueno que te registraste', 'me da gusto hablar contigo'.",
    ]
    if lead_city:
        lines.append(f"- Si es natural, menciona su ciudad/país: '{lead_city}'.")

    # Status context for the AI
    if lead_status == "NEW":
        lines.append(
            "- IMPORTANTE: Este lead es NUEVO. NO tiene boleto general aún. "
            "Tu prioridad es ayudarle a completar su registro."
        )
    elif lead_status == "GENERAL_CONFIRMED":
        lines.append(
            "- Este lead YA tiene su boleto general confirmado. "
            "Enfócate en la experiencia vi-ai-pi."
        )
    elif lead_status in ("VIP_INTERESTED", "VIP_LINK_SENT"):
        lines.append(
            f"- Este lead ya mostró interés en vi-ai-pi (estado: {lead_status}). "
            "Sigue con el cierre de venta."
        )

    # WhatsApp sender info
    if whatsapp_display:
        lines.append(
            f"- Ya le debió de haber llegado información a su teléfono por WhatsApp. "
            f"Puedes decirle: 'revisa tu WhatsApp, ya te mandamos la información'. "
            f"Los mensajes de WhatsApp llegan del número {whatsapp_display}. "
            f"Si te pregunta de dónde, dile ese número para que pueda encontrar los mensajes."
        )
    lines.append("")

    # ── Event data section ──
    lines.append("═══ DATOS DEL EVENTO ═══")
    lines.append(f"Evento: {event_name}")

    # Date range
    start_fmt = _format_date_for_voice(event_start)
    end_fmt = _format_date_for_voice(event_end)
    if start_fmt and end_fmt and start_fmt != end_fmt:
        general_days = event_facts.get("general_days", 3)
        vip_extra_days = event_facts.get("vip_extra_days", 1)
        lines.append(
            f"Fechas: del {start_fmt} al {end_fmt}"
        )
        lines.append(
            f"El evento dura {general_days} días para asistentes GENERALES. "
            f"Los VI-AI-PI tienen {vip_extra_days} DÍA ADICIONAL exclusivo "
            f"(total {general_days + vip_extra_days} días). "
            f"IMPORTANTE: NO digas que el evento es de {general_days + vip_extra_days} días. "
            f"Di que es de {general_days} días y que los VIP tienen un día extra."
        )
    elif start_fmt:
        lines.append(f"Fecha: {start_fmt}")
    if event_place:
        lines.append(f"Lugar: {event_place}")
    if arrival_time:
        lines.append(f"Hora de llegada: {arrival_time}")
    if speakers:
        lines.append(f"Speakers: {speakers}")

    # Transformation / Edificación
    if transformation:
        lines.extend([
            "",
            "═══ EDIFICACIÓN DEL EVENTO (usa esto para emocionar a la persona) ═══",
            transformation,
        ])

    # VIP section — multi-tier pricing from campaign config
    lines.append("")
    lines.append("═══ EXPERIENCIA VI-AI-PI ═══")
    from ..services.stripe_checkout import get_vip_tiers  # lazy import to avoid circular
    _vip_tiers = get_vip_tiers(campaign)
    if _vip_tiers and any(t.get("display_price") for t in _vip_tiers):
        if len(_vip_tiers) == 1:
            _t = _vip_tiers[0]
            lines.append(f"Precio vi-ai-pi: {_t['display_price']}")
        else:
            lines.append("Opciones de vi-ai-pi:")
            for _t in _vip_tiers:
                _lbl = _t.get("label") or f"VIP opción {_t.get('option', '?')}"
                _dprice = _t.get("display_price") or "consultar"
                lines.append(f"  - {_lbl}: {_dprice}")
            lines.append(f"(La opción más popular es: {_vip_tiers[-1].get('label', 'la última')})")
    elif vip_price:
        lines.append(f"Precio vi-ai-pi: ${vip_price} USD")
    else:
        lines.append("NO tienes el precio vi-ai-pi. No lo inventes.")

    vip_includes = event_facts.get("vip_includes") or []
    if vip_includes and isinstance(vip_includes, list) and len(vip_includes) > 0:
        includes_list = ", ".join(str(v) for v in vip_includes)
        lines.append(
            f"Vi-ai-pi incluye EXACTAMENTE y ÚNICAMENTE: {includes_list}. "
            "NO agregues nada más a esta lista."
        )
    else:
        lines.append(
            "NO tienes los beneficios vi-ai-pi. NO inventes. "
            "Di que les enviarás los detalles por WhatsApp."
        )

    # ── Functional rules ──
    lines.extend([
        "",
        "═══ REGLAS CRÍTICAS ═══",
        "- NUNCA inventes información que no aparezca en este prompt.",
        "- Si no sabes algo, di honestamente que le enviarás la info por WhatsApp.",
        "- Cuando el lead muestre interés en vi-ai-pi, USA la función send_vip_whatsapp O send_payment_link(option=1).",
        "- Después de send_vip_whatsapp si devuelve 'sent': di 'Te acabo de enviar un mensaje por WhatsApp con tu link, revísalo.'",
        "- Si devuelve 'template_sent': di 'Te envié un mensaje por WhatsApp, respóndelo para que te mande tu link de pago.'",
        "- Si devuelve 'pending': di 'Escríbenos por WhatsApp y automáticamente te enviamos tu link.'",
        "- Para enviar link de pago de CUALQUIER opción, usa send_payment_link(option=N).",
        "- Para verificar si ya pagó, usa check_payment_status. Puedes llamarla cada ~20 segundos.",
        "- Para enviar un boleto general gratis, usa send_ticket.",
        "- FLUJO DE VENTA EN LLAMADA:",
        "  1. Si el lead quiere comprar: usa send_payment_link con la opción correcta.",
        "  2. Dile 'Te acabo de enviar el link por WhatsApp. Tómate tu tiempo para completar el pago.'",
        "  3. Espera ~20-30 segundos, luego usa check_payment_status para verificar.",
        "  4. Si ya pagó: felicítalo. El boleto se le envía automáticamente por WhatsApp.",
        "  5. Si no ha pagado: dile que no se preocupe, cuando complete el pago recibirá su boleto automáticamente.",
        "- NUNCA menciones email para enviar información.",
        "- Después de despedirte, SIEMPRE usa la función end_call.",
        "",
        "═══ OBJETIVO ═══",
        objective,
        "",
        "═══ FLUJO DE LA LLAMADA ═══",
        "(Recuerda: después de CADA paso, PARA y ESPERA respuesta)",
        flow,
        "",
        "═══ REGLAS ADICIONALES ═══",
        rules,
    ])

    # If campaign has a custom prompt section, append it as additional instructions
    campaign_prompt_override = (campaign.get("ai_voice_system_prompt") or "").strip()
    if campaign_prompt_override and purpose != "custom":
        lines.extend([
            "",
            "═══ INSTRUCCIONES ADICIONALES DEL ORGANIZADOR ═══",
            campaign_prompt_override,
        ])

    return "\n".join(lines)


# ─── AI Voice Session ────────────────────────────────────────────────────────


class AIVoiceSession:
    """Manages a single AI voice conversation via OpenAI Realtime API.

    Lifecycle:
    1. connect() -- opens WebSocket to OpenAI Realtime API
    2. send_audio(chunk) -- forward raw audio from Telnyx to OpenAI
    3. on_audio_delta callback -- gets AI audio to send back to Telnyx
    4. close() -- disconnect and return conversation log
    """

    def __init__(
        self,
        *,
        openai_api_key: str = "",
        model: str = "gpt-4o-realtime-preview",
        voice: str = "alloy",
        system_prompt: str,
        lead_context: dict,
        event_facts: dict,
        language: str = "es",
        on_audio_delta: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]] | None = None,
        # ElevenLabs TTS (optional — overrides OpenAI audio output)
        elevenlabs_api_key: str = "",
        elevenlabs_voice_id: str = "",
        elevenlabs_model_id: str = "eleven_multilingual_v2",
    ):
        self._api_key = openai_api_key or settings.openai_api_key
        self.model = model
        self.voice = voice
        self.system_prompt = system_prompt
        self.lead_context = lead_context
        self.event_facts = event_facts
        self.language = language

        # ElevenLabs config
        self._elevenlabs_api_key = elevenlabs_api_key
        self._elevenlabs_voice_id = elevenlabs_voice_id
        self._elevenlabs_model_id = elevenlabs_model_id
        self._use_elevenlabs = bool(elevenlabs_api_key and elevenlabs_voice_id)

        # Callbacks
        self.on_audio_delta = on_audio_delta
        self.on_transcript = on_transcript

        # Callbacks from caller
        self.on_call_end: Callable[[], Awaitable[None]] | None = None
        self.on_send_vip_whatsapp: Callable[[], Awaitable[dict]] | None = None
        self.on_send_payment_link: Callable[[int], Awaitable[dict]] | None = None
        self.on_check_payment_status: Callable[[], Awaitable[dict]] | None = None
        self.on_send_ticket: Callable[[str], Awaitable[dict]] | None = None

        # Internal state
        self._ws: Any = None
        self._el_ws: Any = None  # ElevenLabs WebSocket
        self._el_listen_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._conversation_log: list[dict[str, str]] = []
        self._current_ai_transcript: list[str] = []
        self._connected = False
        self._ai_speaking = False  # True while AI is outputting audio

    # ── Connect ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open WebSocket to OpenAI Realtime API and configure the session."""
        if not self._api_key:
            raise RuntimeError("Missing OpenAI API key for AI voice session")

        url = OPENAI_REALTIME_URL.format(model=self.model)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        voice_provider = "elevenlabs" if self._use_elevenlabs else "openai"
        logger.info(
            "ai_voice_connecting model=%s voice=%s provider=%s",
            self.model, self.voice, voice_provider,
        )

        self._ws = await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True

        # Connect ElevenLabs WebSocket if using it
        if self._use_elevenlabs:
            await self._connect_elevenlabs()

        # Build enriched instructions with lead context
        instructions = self._build_instructions()

        # If using ElevenLabs: text-only output from OpenAI
        modalities = ["text"] if self._use_elevenlabs else ["text", "audio"]

        session_config: dict[str, Any] = {
                "modalities": modalities,
                "instructions": instructions,
                "input_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.65,
                    "prefix_padding_ms": 400,
                    "silence_duration_ms": 1200,
                },
                "tools": [
                    {
                        "type": "function",
                        "name": "end_call",
                        "description": (
                            "Cuelga la llamada telefónica. SOLO usa esta función después de que "
                            "ya hayas hablado con la persona Y te hayas despedido verbalmente. "
                            "NUNCA la uses al inicio de la llamada ni antes de haber tenido una conversación. "
                            "Primero despídete con voz, y DESPUÉS llama esta función."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "description": "Motivo breve del fin de llamada",
                                }
                            },
                            "required": ["reason"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "send_vip_whatsapp",
                        "description": (
                            "Envía un mensaje por WhatsApp al lead con información VIP. "
                            "Usa esta función cuando el lead muestre interés en VIP y quiera recibir "
                            "el link de pago o más información. La función enviará un mensaje al WhatsApp "
                            "del lead automáticamente. Después de llamar esta función, dile al lead: "
                            "'Te acabo de enviar un mensaje por WhatsApp, revísalo y respóndelo para "
                            "que te pueda enviar tu link de pago.'"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "description": "Motivo del envío (e.g. 'lead interesado en VIP')",
                                }
                            },
                            "required": ["reason"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "send_payment_link",
                        "description": (
                            "Envía un link de pago por WhatsApp al lead. "
                            "Usa esta función cuando el lead quiera comprar un boleto VIP u otra opción de pago. "
                            "Especifica la opción de pago (1, 2, etc). "
                            "Después de llamar esta función, dile al lead: "
                            "'Te acabo de enviar el link de pago por WhatsApp, tómate tu tiempo para completarlo.'"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "option": {
                                    "type": "integer",
                                    "description": "Opción de pago (1 = VIP individual, 2 = 2 VIPs promo, etc.)",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Motivo del envío",
                                },
                            },
                            "required": ["option", "reason"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "check_payment_status",
                        "description": (
                            "Verifica si el lead ya completó su pago. "
                            "Usa esta función después de haber enviado un link de pago y quieras verificar "
                            "si ya pagó. Si devuelve 'paid', felicita al lead y dile que su boleto "
                            "le llegará por WhatsApp automáticamente. Si devuelve 'pending', "
                            "dile que aún no se refleja el pago y que se tome su tiempo."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "description": "Motivo de la verificación",
                                },
                            },
                            "required": ["reason"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "send_ticket",
                        "description": (
                            "Genera y envía un boleto general (gratis) por WhatsApp al lead. "
                            "Usa esta función cuando quieras confirmar la asistencia del lead "
                            "y enviarle su boleto general sin necesidad de pago."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "tier": {
                                    "type": "string",
                                    "description": "Tipo de boleto. Por defecto GENERAL.",
                                    "enum": ["GENERAL"],
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Motivo del envío",
                                },
                            },
                            "required": ["reason"],
                        },
                    },
                ],
        }

        # Only include voice/audio output config for OpenAI native audio mode
        if not self._use_elevenlabs:
            session_config["voice"] = self.voice
            session_config["output_audio_format"] = "pcm16"

        session_update = {"type": "session.update", "session": session_config}

        await self._ws.send(json.dumps(session_update))
        logger.info("ai_voice_session_update_sent provider=%s", voice_provider)

        # Start background listener
        self._listen_task = asyncio.create_task(
            self._listen_loop(), name="ai_voice_listen"
        )

        # Trigger initial AI greeting — the AI should speak first
        await self._ws.send(json.dumps({"type": "response.create"}))
        logger.info("ai_voice_initial_response_triggered")

    def _build_instructions(self) -> str:
        """Combine system prompt with lead context for session instructions."""
        ctx = self.lead_context or {}
        parts = [self.system_prompt]

        context_lines = ["\nContexto del lead:"]
        if ctx.get("name"):
            context_lines.append(f"- Nombre: {ctx['name']}")
        if ctx.get("phone"):
            context_lines.append(f"- Teléfono: {ctx['phone']}")
        if ctx.get("status"):
            context_lines.append(f"- Status: {ctx['status']}")
        if ctx.get("tier_interest"):
            context_lines.append(f"- Interés de tier: {ctx['tier_interest']}")
        if ctx.get("email"):
            context_lines.append(f"- Email: {ctx['email']}")

        # Add any extra context fields
        skip_keys = {"name", "phone", "status", "tier_interest", "email"}
        for k, v in ctx.items():
            if k not in skip_keys and v:
                context_lines.append(f"- {k}: {v}")

        if len(context_lines) > 1:
            parts.append("\n".join(context_lines))

        # Event facts
        ef = self.event_facts or {}
        if ef:
            event_lines = ["\nDatos del evento:"]
            for k, v in ef.items():
                if v:
                    label = k.replace("_", " ").title()
                    event_lines.append(f"- {label}: {v}")
            if len(event_lines) > 1:
                parts.append("\n".join(event_lines))

        # Language instruction
        if self.language and self.language != "es":
            parts.append(f"\nIdioma de la conversación: {self.language}")

        return "\n".join(parts)

    # ── ElevenLabs TTS ──────────────────────────────────────────────────

    async def _connect_elevenlabs(self) -> None:
        """Open WebSocket to ElevenLabs streaming TTS."""
        el_url = ELEVENLABS_WS_URL.format(
            voice_id=self._elevenlabs_voice_id,
            model_id=self._elevenlabs_model_id,
        )
        logger.info(
            "elevenlabs_connecting voice=%s model=%s",
            self._elevenlabs_voice_id[:12],
            self._elevenlabs_model_id,
        )
        self._el_ws = await websockets.connect(
            el_url, ping_interval=20, ping_timeout=10, close_timeout=5,
        )

        # Send BOS (beginning-of-stream) message
        bos = {
            "text": " ",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "use_speaker_boost": True,
            },
            "xi_api_key": self._elevenlabs_api_key,
        }
        await self._el_ws.send(json.dumps(bos))
        logger.info("elevenlabs_bos_sent")

        # Start ElevenLabs listener
        self._el_listen_task = asyncio.create_task(
            self._elevenlabs_listen_loop(), name="elevenlabs_listen",
        )

    async def _elevenlabs_listen_loop(self) -> None:
        """Read audio chunks from ElevenLabs and forward to Telnyx."""
        try:
            async for raw_msg in self._el_ws:
                try:
                    data = json.loads(raw_msg)
                    audio_b64 = data.get("audio")
                    is_final = data.get("isFinal", False)

                    if audio_b64:
                        # ElevenLabs returns ulaw_8000 base64 → send directly to Telnyx
                        self._ai_speaking = True
                        await self.on_audio_delta(audio_b64.encode("ascii"))

                    if is_final:
                        self._ai_speaking = False
                        logger.info("elevenlabs_stream_final — reconnecting for next utterance")
                        # Clear OpenAI input buffer to discard echo
                        if self._ws:
                            try:
                                await self._ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                            except Exception:
                                pass
                        # ElevenLabs WS is done after isFinal — reconnect for next response
                        await self._reconnect_elevenlabs()

                except json.JSONDecodeError:
                    pass
                except Exception as exc:
                    logger.error("elevenlabs_event_error err=%s", str(exc)[:200])
        except websockets.ConnectionClosedOK:
            logger.info("elevenlabs_ws_closed_ok")
        except websockets.ConnectionClosedError as exc:
            logger.warning("elevenlabs_ws_closed_error code=%s", exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("elevenlabs_listen_error err=%s", str(exc)[:200])

    async def _reconnect_elevenlabs(self) -> None:
        """Close the current ElevenLabs WS and open a fresh one for the next utterance.

        ElevenLabs streaming WS is single-use: after flush + isFinal, we need
        a new connection for the next piece of audio.
        """
        old_ws = self._el_ws
        self._el_ws = None  # Prevent sends to closed WS

        if old_ws:
            try:
                await old_ws.close()
            except Exception:
                pass

        try:
            await self._connect_elevenlabs()
            logger.info("elevenlabs_reconnected")
        except Exception as exc:
            logger.error("elevenlabs_reconnect_failed err=%s", str(exc)[:200])

    async def _send_text_to_elevenlabs(self, text: str) -> None:
        """Stream a text chunk to ElevenLabs for TTS conversion."""
        if not self._el_ws:
            return
        try:
            await self._el_ws.send(json.dumps({
                "text": text,
                "try_trigger_generation": True,
            }))
        except Exception as exc:
            logger.error("elevenlabs_send_failed err=%s", str(exc)[:200])

    async def _flush_elevenlabs(self) -> None:
        """Send EOS (end-of-stream) to ElevenLabs to flush remaining audio."""
        if not self._el_ws:
            return
        try:
            await self._el_ws.send(json.dumps({"text": ""}))
        except Exception as exc:
            logger.error("elevenlabs_flush_failed err=%s", str(exc)[:200])

    # ── Send audio ───────────────────────────────────────────────────────

    async def send_audio(self, audio_chunk: str | bytes) -> None:
        """Forward audio from Telnyx to OpenAI.

        audio_chunk: base64-encoded mulaw 8kHz mono from Telnyx.
        Pipeline: base64 decode -> mulaw->PCM16 -> resample 8k->24k -> base64 -> send
        """
        if not self._connected or not self._ws:
            return

        # Suppress echo: don't forward inbound audio while AI is speaking
        if self._ai_speaking:
            return

        try:
            # Ensure we have a string for length/padding checks
            chunk_str = audio_chunk if isinstance(audio_chunk, str) else audio_chunk.decode("ascii")

            # Skip tiny/empty chunks (Telnyx sometimes sends 1-char garbage)
            if len(chunk_str) < 4:
                return

            # 1. Decode base64 to raw mulaw bytes (fix padding if needed)
            missing_padding = len(chunk_str) % 4
            if missing_padding:
                chunk_str += "=" * (4 - missing_padding)
            raw_mulaw = base64.b64decode(chunk_str)

            if not raw_mulaw:
                return

            # 2. Convert mulaw -> PCM16 (16-bit signed, little-endian)
            pcm16_8k = audioop.ulaw2lin(raw_mulaw, 2)

            # 3. Resample 8kHz -> 24kHz
            pcm16_24k = _resample_8k_to_24k(pcm16_8k)

            # 4. Base64-encode the PCM16 24kHz data
            b64_pcm16_24k = base64.b64encode(pcm16_24k).decode("ascii")

            # 5. Send to OpenAI
            msg = {
                "type": "input_audio_buffer.append",
                "audio": b64_pcm16_24k,
            }
            await self._ws.send(json.dumps(msg))

        except Exception as exc:
            logger.error("send_audio_failed err=%s", str(exc)[:300])

    # ── Listen loop ──────────────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Background task reading events from OpenAI Realtime WebSocket."""
        try:
            async for raw_msg in self._ws:
                try:
                    event = json.loads(raw_msg)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    logger.warning("ai_voice_invalid_json len=%d", len(raw_msg))
                except Exception as exc:
                    logger.error(
                        "ai_voice_event_handler_error err=%s", str(exc)[:300]
                    )
        except websockets.ConnectionClosedOK:
            logger.info("ai_voice_ws_closed_ok")
        except websockets.ConnectionClosedError as exc:
            logger.warning(
                "ai_voice_ws_closed_error code=%s reason=%s",
                exc.code,
                str(exc.reason)[:200],
            )
        except asyncio.CancelledError:
            logger.info("ai_voice_listen_cancelled")
            raise
        except Exception as exc:
            logger.error("ai_voice_listen_loop_error err=%s", str(exc)[:300])
        finally:
            self._connected = False

    async def _handle_event(self, event: dict) -> None:
        """Dispatch a single OpenAI Realtime event."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            session_id = event.get("session", {}).get("id", "?")
            logger.info("ai_voice_session_created session_id=%s", session_id)

        elif event_type == "session.updated":
            logger.info("ai_voice_session_updated")

        # ── ElevenLabs text-mode events ──
        elif event_type == "response.text.delta" and self._use_elevenlabs:
            # Stream text chunk to ElevenLabs for TTS
            delta = event.get("delta", "")
            if delta:
                self._current_ai_transcript.append(delta)
                await self._send_text_to_elevenlabs(delta)

        elif event_type == "response.text.done" and self._use_elevenlabs:
            # Flush ElevenLabs and log the complete transcript
            await self._flush_elevenlabs()
            transcript = event.get("text", "")
            if not transcript:
                transcript = "".join(self._current_ai_transcript)
            self._current_ai_transcript.clear()

            if transcript.strip():
                self._conversation_log.append(
                    {
                        "role": "assistant",
                        "text": transcript.strip(),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                logger.info("ai_voice_ai_turn text=%s", transcript.strip()[:120])
                if self.on_transcript:
                    try:
                        await self.on_transcript("assistant", transcript.strip())
                    except Exception as exc:
                        logger.error("on_transcript_callback_error err=%s", str(exc)[:200])

        # ── OpenAI native audio events ──
        elif event_type == "response.audio.delta" and not self._use_elevenlabs:
            self._ai_speaking = True
            await self._handle_audio_delta(event)

        elif event_type == "response.audio.done" and not self._use_elevenlabs:
            self._ai_speaking = False
            # Clear input audio buffer to discard any echo captured during AI speech
            try:
                await self._ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            except Exception:
                pass

        elif event_type == "response.audio_transcript.delta" and not self._use_elevenlabs:
            # Accumulate partial AI transcript
            delta_text = event.get("delta", "")
            if delta_text:
                self._current_ai_transcript.append(delta_text)

        elif event_type == "response.audio_transcript.done" and not self._use_elevenlabs:
            # Complete AI turn transcript
            transcript = event.get("transcript", "")
            if not transcript:
                transcript = "".join(self._current_ai_transcript)
            self._current_ai_transcript.clear()

            if transcript.strip():
                self._conversation_log.append(
                    {
                        "role": "assistant",
                        "text": transcript.strip(),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                logger.info(
                    "ai_voice_ai_turn text=%s",
                    transcript.strip()[:120],
                )
                if self.on_transcript:
                    try:
                        await self.on_transcript("assistant", transcript.strip())
                    except Exception as exc:
                        logger.error(
                            "on_transcript_callback_error err=%s", str(exc)[:200]
                        )

        elif event_type == "conversation.item.input_audio_transcription.completed":
            # User speech transcript
            user_text = (event.get("transcript") or "").strip()
            if user_text:
                self._conversation_log.append(
                    {
                        "role": "user",
                        "text": user_text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                logger.info("ai_voice_user_turn text=%s", user_text[:120])
                if self.on_transcript:
                    try:
                        await self.on_transcript("user", user_text)
                    except Exception as exc:
                        logger.error(
                            "on_transcript_callback_error err=%s", str(exc)[:200]
                        )

        elif event_type == "response.done":
            usage = event.get("response", {}).get("usage", {})
            logger.info("ai_voice_response_done usage=%s", json.dumps(usage))

            # Check for function calls in the response output
            for item in event.get("response", {}).get("output", []):
                if item.get("type") != "function_call":
                    continue

                func_name = item.get("name", "")
                call_id = item.get("call_id", "")

                if func_name == "send_vip_whatsapp":
                    args_str = item.get("arguments", "{}")
                    logger.info("ai_voice_send_vip_whatsapp_requested args=%s", args_str)

                    result = {"status": "error", "message": "No WhatsApp callback configured"}
                    if self.on_send_vip_whatsapp:
                        try:
                            result = await self.on_send_vip_whatsapp()
                        except Exception as exc:
                            logger.error("send_vip_whatsapp_error err=%s", str(exc)[:200])
                            result = {"status": "error", "message": str(exc)[:100]}

                    # Send function call output
                    if call_id:
                        try:
                            await self._ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result, ensure_ascii=False),
                                },
                            }))
                            await self._ws.send(json.dumps({"type": "response.create"}))
                        except Exception:
                            pass
                    continue

                if func_name == "send_payment_link":
                    args_str = item.get("arguments", "{}")
                    logger.info("ai_voice_send_payment_link_requested args=%s", args_str)
                    args = {}
                    try:
                        args = json.loads(args_str)
                    except Exception:
                        pass
                    option = args.get("option", 1)

                    result = {"status": "error", "message": "No payment link callback configured"}
                    if self.on_send_payment_link:
                        try:
                            result = await self.on_send_payment_link(option)
                        except Exception as exc:
                            logger.error("send_payment_link_error err=%s", str(exc)[:200])
                            result = {"status": "error", "message": str(exc)[:100]}

                    if call_id:
                        try:
                            await self._ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result, ensure_ascii=False),
                                },
                            }))
                            await self._ws.send(json.dumps({"type": "response.create"}))
                        except Exception:
                            pass
                    continue

                if func_name == "check_payment_status":
                    args_str = item.get("arguments", "{}")
                    logger.info("ai_voice_check_payment_status_requested args=%s", args_str)

                    result = {"status": "error", "message": "No payment status callback configured"}
                    if self.on_check_payment_status:
                        try:
                            result = await self.on_check_payment_status()
                        except Exception as exc:
                            logger.error("check_payment_status_error err=%s", str(exc)[:200])
                            result = {"status": "error", "message": str(exc)[:100]}

                    if call_id:
                        try:
                            await self._ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result, ensure_ascii=False),
                                },
                            }))
                            await self._ws.send(json.dumps({"type": "response.create"}))
                        except Exception:
                            pass
                    continue

                if func_name == "send_ticket":
                    args_str = item.get("arguments", "{}")
                    logger.info("ai_voice_send_ticket_requested args=%s", args_str)
                    args = {}
                    try:
                        args = json.loads(args_str)
                    except Exception:
                        pass
                    tier = args.get("tier", "GENERAL")

                    result = {"status": "error", "message": "No ticket callback configured"}
                    if self.on_send_ticket:
                        try:
                            result = await self.on_send_ticket(tier)
                        except Exception as exc:
                            logger.error("send_ticket_error err=%s", str(exc)[:200])
                            result = {"status": "error", "message": str(exc)[:100]}

                    if call_id:
                        try:
                            await self._ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result, ensure_ascii=False),
                                },
                            }))
                            await self._ws.send(json.dumps({"type": "response.create"}))
                        except Exception:
                            pass
                    continue

                if func_name == "end_call":
                    args_str = item.get("arguments", "{}")
                    logger.info("ai_voice_end_call_requested args=%s turns=%d", args_str, len(self._conversation_log))

                    # Safeguard: don't hang up if no real conversation happened
                    if len(self._conversation_log) < 2:
                        logger.warning("ai_voice_end_call_too_early turns=%d — ignoring", len(self._conversation_log))
                        # Send rejection and ask AI to keep going
                        if call_id:
                            try:
                                await self._ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": '{"status": "rejected", "reason": "No puedes colgar todavía. Primero saluda y habla con la persona."}',
                                    },
                                }))
                                await self._ws.send(json.dumps({"type": "response.create"}))
                            except Exception:
                                pass
                        continue

                    # Send function call output to satisfy OpenAI
                    if call_id:
                        try:
                            await self._ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": '{"status": "call_ended"}',
                                },
                            }))
                        except Exception:
                            pass

                    # Trigger call end via callback
                    if self.on_call_end:
                        try:
                            await self.on_call_end()
                        except Exception as exc:
                            logger.error("on_call_end_error err=%s", str(exc)[:200])

        elif event_type == "error":
            error_body = event.get("error", {})
            logger.error(
                "ai_voice_openai_error type=%s code=%s message=%s",
                error_body.get("type", "?"),
                error_body.get("code", "?"),
                str(error_body.get("message", ""))[:300],
            )

        elif event_type == "input_audio_buffer.speech_started":
            # User started talking — stop AI audio playback
            if self._ai_speaking:
                self._ai_speaking = False
                logger.info("speech_interruption — stopping ElevenLabs playback")
                if self._use_elevenlabs:
                    # Cancel old listener before reconnecting
                    if self._el_listen_task and not self._el_listen_task.done():
                        self._el_listen_task.cancel()
                    await self._reconnect_elevenlabs()

        elif event_type in (
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "response.created",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "rate_limits.updated",
            "conversation.item.created",
            "response.text.delta",
            "response.text.done",
            "response.audio.delta",
            "response.audio.done",
            "response.audio_transcript.delta",
            "response.audio_transcript.done",
        ):
            # Known events we don't need to act on (or already handled above)
            pass

        else:
            logger.debug("ai_voice_unhandled_event type=%s", event_type)

    async def _handle_audio_delta(self, event: dict) -> None:
        """Process an audio delta from OpenAI and forward to Telnyx.

        Pipeline: base64 decode -> resample 24k->8k -> PCM16->mulaw -> base64 -> callback
        """
        b64_audio = event.get("delta", "")
        if not b64_audio:
            return

        try:
            # 1. Decode base64 to raw PCM16 24kHz
            pcm16_24k = base64.b64decode(b64_audio)

            # 2. Resample 24kHz -> 8kHz
            pcm16_8k = _resample_24k_to_8k(pcm16_24k)

            # 3. Convert PCM16 -> mulaw
            mulaw_8k = audioop.lin2ulaw(pcm16_8k, 2)

            # 4. Base64-encode for Telnyx
            b64_mulaw = base64.b64encode(mulaw_8k).decode("ascii")

            # 5. Invoke the callback
            await self.on_audio_delta(b64_mulaw.encode("ascii"))

        except Exception as exc:
            logger.error("audio_delta_processing_failed err=%s", str(exc)[:300])

    # ── Close ────────────────────────────────────────────────────────────

    async def close(self) -> list[dict[str, str]]:
        """Disconnect and return the conversation log.

        Returns list of {"role": "user"|"assistant", "text": "...", "timestamp": "..."}.
        """
        logger.info(
            "ai_voice_closing turns=%d", len(self._conversation_log)
        )

        # Cancel the listen tasks
        for task in [self._listen_task, self._el_listen_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close WebSockets
        for ws, label in [(self._ws, "openai"), (self._el_ws, "elevenlabs")]:
            if ws:
                try:
                    await ws.close()
                except Exception as exc:
                    logger.warning("%s_ws_close_error err=%s", label, str(exc)[:200])

        self._connected = False
        self._ws = None
        self._el_ws = None
        self._listen_task = None
        self._el_listen_task = None

        logger.info(
            "ai_voice_closed total_turns=%d", len(self._conversation_log)
        )
        return list(self._conversation_log)

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._connected

    @property
    def conversation_log(self) -> list[dict[str, str]]:
        """Current conversation log (read-only snapshot)."""
        return list(self._conversation_log)
