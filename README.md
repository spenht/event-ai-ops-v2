# Event AI Ops v2 (WhatsApp + IA + Stripe + QR)

Este repo es una versión limpia enfocada a **WhatsApp**:
- Conversación 100% por IA (prompt principal)
- Venta de VIP con **Stripe Checkout**
- Cuando el pago se confirma (Stripe webhook) se manda **boleto VIP con QR** por WhatsApp
- Si confirma asistencia GENERAL, se manda **boleto General con QR**

## 1) Variables de entorno (Fly secrets)

Obligatorias:
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM` (ej. `whatsapp:+14155238886` o tu número aprobado)
- `OPENAI_API_KEY`
- `PUBLIC_BASE_URL` (ej. `https://calls-mx.fly.dev`)

Para VIP + pagos:
- `STRIPE_SECRET_KEY` (LIVE)
- `STRIPE_WEBHOOK_SECRET` (LIVE)
- `STRIPE_VIP_PRICE_ID` (LIVE)
- `STRIPE_SUCCESS_URL`
- `STRIPE_CANCEL_URL`

Media (opcional pero recomendado):
- `WHATSAPP_VIDEO_VIP_PITCH` (URL **https pública** a mp4)

Evento (opcional):
- `EVENT_NAME`
- `EVENT_DATE`
- `EVENT_PLACE`
- `EVENT_SPEAKERS`

## 2) Correr local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Probar health:
```bash
curl http://127.0.0.1:8080/health
```

## 3) Deploy a Fly

Desde la carpeta del repo v2:
```bash
fly deploy --app calls-mx
```

Configurar secrets (ejemplo):
```bash
fly secrets set --app calls-mx \
  PUBLIC_BASE_URL="https://calls-mx.fly.dev" \
  SUPABASE_URL="..." SUPABASE_KEY="..." \
  TWILIO_ACCOUNT_SID="..." TWILIO_AUTH_TOKEN="..." TWILIO_WHATSAPP_FROM="whatsapp:+14155238886" \
  OPENAI_API_KEY="..." OPENAI_MODEL="gpt-4o-mini" \
  STRIPE_SECRET_KEY="sk_live_..." STRIPE_WEBHOOK_SECRET="whsec_..." STRIPE_VIP_PRICE_ID="price_..." \
  STRIPE_SUCCESS_URL="https://tu-dominio.com/vip/success" \
  STRIPE_CANCEL_URL="https://tu-dominio.com/vip/cancel" \
  WHATSAPP_VIDEO_VIP_PITCH="https://.../vip.mp4"
```

Ver logs:
```bash
fly logs --app calls-mx
```

## 4) Twilio (WhatsApp) Webhook

En Twilio (WhatsApp sender):
- **WHEN A MESSAGE COMES IN** →
  `https://calls-mx.fly.dev/v1/messaging/whatsapp/inbound`

> Nota: este endpoint responde **TwiML vacío** y el envío real se hace por REST (para soportar media).

## 5) Stripe Webhook

En Stripe (LIVE) crea un webhook apuntando a:
- `https://calls-mx.fly.dev/v1/payments/stripe/webhook`

Eventos mínimos:
- `checkout.session.completed`

Copia el signing secret a `STRIPE_WEBHOOK_SECRET`.

## 6) Pruebas rápidas

### Simular inbound (como Twilio)
```bash
curl -i -X POST https://calls-mx.fly.dev/v1/messaging/whatsapp/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "From=whatsapp:+5215555555555" \
  --data-urlencode "To=whatsapp:+14155238886" \
  --data-urlencode "Body=hola" \
  --data-urlencode "MessageSid=SM_TEST_1"
```

### Crear link VIP manual (debug)
```bash
curl -s -X POST https://calls-mx.fly.dev/v1/payments/create-link \
  -H "Content-Type: application/json" \
  -d '{"lead_id":"LEAD_ID","tier":"VIP"}' | jq
```

## 7) Notas importantes (para que SÍ mande media)

- `WHATSAPP_VIDEO_VIP_PITCH` debe ser **https público** (Twilio lo descarga desde sus servers).
- Los boletos QR se sirven desde este mismo app en:
  `/v1/tickets/{ticket_id}.png?t={token}`
- Si escalas a múltiples máquinas, conviene usar un bucket (Supabase Storage) o un Fly Volume.

