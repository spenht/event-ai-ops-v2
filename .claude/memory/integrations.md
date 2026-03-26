---
name: Integration Map
description: How external APIs connect - Twilio, Telnyx, Stripe, OpenAI, ElevenLabs, Meta CAPI
type: reference
---

# Mapa de Integraciones

## Twilio WhatsApp
- **Uso:** Mensajes inbound/outbound, media (imágenes, video)
- **Multi-tenant:** Credenciales por campaña o fallback global
- **Webhook:** `POST /v1/whatsapp/inbound` (no tiene verificación de firma Twilio)
- **Archivos:** routes/whatsapp.py, services/twilio_whatsapp.py

## Telnyx Voice
- **Uso:** Llamadas outbound a leads (SIP), grabaciones
- **Endpoint:** `https://api.telnyx.com/v2`
- **Auth:** Bearer token per-campaign o global
- **Webhook:** routes/telnyx_webhooks.py (sin verificación de firma)
- **WebSocket:** routes/call_media_ws.py (audio streaming bidireccional)
- **Archivos:** services/telnyx_calls.py, services/number_pool.py

## Stripe (Dual Mode)
- **Directo:** Checkout session con secret key del proyecto
- **Connect:** Platform model, cada org tiene su cuenta Express
- **Webhook:** `checkout.session.completed` → genera ticket + atribuye comisión
- **IMPORTANTE:** Si no hay webhook secret configurado, acepta JSON sin verificar firma
- **Archivos:** services/stripe_checkout.py, services/stripe_connect.py, routes/payments.py, routes/stripe_connect.py

## OpenAI
- **Endpoint correcto:** `https://api.openai.com/v1/chat/completions`
- **NUNCA usar:** `/v1/responses` (no existe)
- **Payload:** `messages` (no `input`), `max_tokens` (no `max_output_tokens`)
- **Response:** `data["choices"][0]["message"]["content"]`
- **Modelo default:** gpt-4o-mini (configurable por campaña)
- **Realtime API:** Para voice calls (WebSocket, diferente al chat)
- **Archivos:** services/openai_chat.py, services/ai_voice.py, services/post_call_processor.py

## ElevenLabs TTS
- **Uso:** Síntesis de voz para AI voice calls
- **Configurable:** voice_id por campaña
- **Archivos:** services/ai_voice.py

## Meta Conversions API (CAPI)
- **Uso:** Server-side tracking (Lead, Purchase events)
- **PII hasheado:** email, phone
- **Fire-and-forget:** errores no bloquean el flujo
- **Archivos:** services/meta_conversions.py

## Google Sheets
- **Uso:** Backup/reporting legacy, sync de leads
- **Fire-and-forget:** `except: pass` en todos los calls
- **Archivos:** services/google_sheets.py

## Supabase
- **DB:** PostgreSQL via supabase-py
- **Storage:** Media uploads (buckets: whatsapp, assets, media)
- **Cliente compartido:** `from ..deps import sb`
- **Service role key:** Solo en media.py upload (riesgo de seguridad conocido)
