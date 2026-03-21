# CLAUDE.md — Event AI Ops v2

## Qué es este proyecto

Plataforma de gestión de eventos con IA: chatbot WhatsApp (OpenAI), pagos (Stripe), boletos con QR, llamadas de voz (Telnyx/WebRTC), comisiones para agentes de venta (Spartans), y seguimientos automáticos. Backend en **FastAPI** desplegado en **Fly.io**, con **Supabase** (PostgreSQL) como base de datos.

## Stack técnico

- **Python 3.11** / **FastAPI** 0.115.6 / **Uvicorn**
- **Supabase** — Base de datos y auth
- **Stripe** — Pagos + Stripe Connect
- **Twilio** — WhatsApp (envío y recepción)
- **OpenAI** — Chat Completions (WhatsApp bot) + Realtime API (voz)
- **Telnyx** — Llamadas SIP/WebRTC
- **ElevenLabs** — TTS (opcional)
- **Pillow + qrcode** — Generación de boletos PNG (1080×1920)
- **Google Sheets / Meta Conversions API** — Integraciones auxiliares

## Estructura del proyecto

```
app/
├── main.py                  # FastAPI app, registro de routers, health check
├── settings.py              # Configuración (frozen dataclass, env vars + DB overrides)
├── deps.py                  # Dependency injection (Supabase client)
├── prompts/
│   └── whatsapp_system_prompt.txt   # System prompt del chatbot (Ana)
├── routes/                  # 20 routers (~11k líneas)
│   ├── whatsapp.py          # Inbound WhatsApp, flujo de conversación IA
│   ├── payments.py          # Stripe Checkout + webhook
│   ├── tickets.py           # Servir boletos, endpoint de diseño IA
│   ├── automation.py        # Follow-ups automáticos (cron cada 5 min)
│   ├── calls_api.py         # Cola de llamadas, asignación a agentes
│   ├── webrtc_api.py        # WebRTC para voz
│   ├── call_media_ws.py     # WebSocket media streaming
│   ├── telnyx_webhooks.py   # Webhooks de voz
│   ├── broadcasts.py        # Mensajes masivos
│   ├── lead_capture.py      # Captura de leads (landing pages)
│   ├── landing_pages.py     # Generación dinámica de landing pages
│   ├── ticket_issue.py      # Generación manual de boletos
│   ├── stripe_connect.py    # Stripe Connect (plataforma)
│   ├── commissions.py       # Comisiones y payouts
│   ├── spartans.py          # Gestión de agentes de venta
│   ├── spartan_dashboard.py # Dashboard de analytics para agentes
│   ├── traffic_sources.py   # Tracking UTM
│   ├── payment_verification.py # Verificación de pagos
│   ├── short_urls.py        # Acortador de URLs
│   ├── checkin.py           # Check-in en evento
│   └── media.py             # Servir media
└── services/                # Lógica de negocio (~8k líneas)
    ├── tickets.py           # Generación QR + rendering PNG
    ├── openai_chat.py       # Conversación IA, extracción de tokens
    ├── twilio_whatsapp.py   # Cliente REST WhatsApp
    ├── stripe_checkout.py   # Creación de sesiones Stripe
    ├── ai_voice.py          # OpenAI Realtime + ElevenLabs TTS
    ├── telnyx_calls.py      # Gestión de llamadas
    ├── number_pool.py       # Pool de números telefónicos
    ├── call_queue.py        # Lógica de cola + tracking de sesiones
    ├── commission_engine.py # Atribución de ventas, payouts
    ├── post_call_processor.py # Transcripción post-llamada
    ├── delayed_call_scheduler.py # Programación async de llamadas
    ├── google_sheets.py     # Integración Google Sheets
    ├── meta_conversions.py  # Facebook Conversions API
    ├── whatsapp_templates.py # Mensajería basada en templates
    ├── url_shortener.py     # Acortador de URLs
    └── stripe_connect.py    # Flujo Stripe Connect
```

## Comandos esenciales

```bash
# Setup local
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Correr servidor
uvicorn app.main:app --reload --port 8080

# Health check
curl http://127.0.0.1:8080/health

# Deploy
fly deploy --app calls-mx

# Logs
fly logs --app calls-mx

# Tests (standalone, genera PNGs de prueba)
python test_ticket_design.py
```

## Arquitectura y patrones clave

- **Async-first**: Rutas async, httpx para APIs externas, `asyncio.create_task()` para fire-and-forget.
- **Configuración en cascada**: Campaign DB → Environment → Defaults (ver `settings.py`).
- **Webhook-driven**: Twilio (WhatsApp), Stripe (pagos), Telnyx (voz).
- **Tokens de acción en IA**: El chatbot emite tokens como `[[SEND_VIP_LINK]]`, `[[SEND_VIP_VIDEO]]`, `[[SEND_GENERAL_TICKET]]` que disparan acciones.
- **Multi-campaña**: Cada ruta soporta `campaign_id` con overrides por campaña en BD.
- **Helpers privados**: Prefijo `_` para funciones internas.
- **Logging**: `logging.getLogger(__name__)` en cada módulo.

## Convenciones de código

- Type hints con `from __future__ import annotations`
- snake_case para funciones/variables, PascalCase para clases
- Frozen dataclasses para configuración
- Supabase queries encadenadas: `.select().eq().limit().execute()`
- Try/except con `logger.exception()` para errores
- Sin linter/formatter configurado actualmente

## Tablas principales (Supabase)

- `leads` — Registros de asistentes (status, tier_interest, payment_status)
- `campaigns` — Configuración por campaña (Stripe keys, Twilio creds, ticket_config)
- `events` — Metadata de eventos
- `touchpoints` — Log de interacciones
- `tickets` — Registros de boletos (ticket_id, tier, file_path, token)
- `call_records` — Historial de llamadas
- `commission_configs` / `commissions` — Reglas y registros de comisiones
- `orgs` — Organizaciones (Stripe Connect)

## Variables de entorno

Copiar `.env.example` a `.env`. Las principales:
- `SUPABASE_URL`, `SUPABASE_KEY` — Obligatorias
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` — WhatsApp
- `OPENAI_API_KEY`, `OPENAI_MODEL` — IA
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_VIP_PRICE_ID` — Pagos
- `PUBLIC_BASE_URL` — Dominio público del app
- `CRON_TOKEN`, `SPARTANS_KEY`, `CHECKIN_KEY` — Auth tokens

> La mayoría de credenciales pueden configurarse per-campaign en la tabla `campaigns` de Supabase, que tiene prioridad sobre env vars.

## Endpoints principales

| Método | Ruta | Propósito |
|--------|------|-----------|
| POST | `/v1/messaging/whatsapp/inbound` | Webhook Twilio (WhatsApp) |
| POST | `/v1/payments/create-link` | Crear sesión Stripe Checkout |
| POST | `/v1/payments/stripe/webhook` | Webhook Stripe (pago confirmado) |
| GET  | `/v1/tickets/{ticket_id}.png?t={token}` | Servir boleto PNG |
| POST | `/v1/automation/followups` | Ejecutar follow-ups (cron) |
| POST | `/v1/calls/enqueue` | Encolar llamada |
| POST | `/v1/broadcasts/campaign` | Mensajes masivos |
| GET  | `/health` | Health check |

## Notas para desarrollo

- **No hay CI/tests formales** — solo `test_ticket_design.py` como script standalone.
- **No hay linter/formatter** configurado (considerar ruff/black en el futuro).
- **CORS abierto** — `allow_origins=["*"]` en main.py.
- Los boletos se guardan en `/tmp/tickets/` (o `TICKETS_DIR`). En multi-instancia considerar Supabase Storage o Fly Volume.
- El chatbot WhatsApp (Ana) tiene un prompt extenso en `app/prompts/whatsapp_system_prompt.txt` con reglas de flujo conversacional.
