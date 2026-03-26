---
name: Customer Journey
description: Complete lead lifecycle from capture to ticket delivery and commission attribution
type: project
---

# Customer Journey

## Lead Status Lifecycle

```
NEW → GENERAL_CONFIRMED → VIP_INTERESTED → VIP_LINK_SENT → PAID
                                                            ↓
                                                    DO_NOT_CONTACT
```

## Flujo detallado

### 1. Lead Capture
- Landing pages, Meta Ads click-to-WhatsApp, formularios embeddables
- Endpoint público: `POST /v1/leads/capture`
- Tracking UTM completo → traffic_sources

### 2. Contacto Inicial (WhatsApp AI)
- Lead auto-creado al primer mensaje de WhatsApp
- AI saluda en español (personaje configurable, default "Ana")
- Pide confirmación de nombre + email
- Asigna status GENERAL (tier gratuito)

### 3. Calificación + Engagement
- AI entiende intent del lead desde la conversación
- Auto-envía ticket QR de GENERAL si confirma asistencia
- Follow-ups automatizados: 15min → 1hr → diario
- Envía videos de testimonios y beneficios VIP

### 4. VIP Upsell
- AI genera link de Stripe Checkout
- Dos opciones: VIP individual ($79) o paquete 2 VIP ($97)
- Link enviado por WhatsApp

### 5. Pago + Entrega
- Stripe webhook `checkout.session.completed` dispara:
  - Generación de ticket PNG con QR
  - Envío por WhatsApp
  - Update status → PAID
  - Atribución de comisión (si Spartan involucrado)
  - Meta CAPI purchase event
  - Sync Google Sheets

### 6. Follow-up
- Broadcasts por WhatsApp (templates aprobados por Meta)
- Reminders automatizados
- Touchpoint logging de toda interacción

**Why:** Entender este flujo es crítico para no romper la cadena de conversión.

**How to apply:** Cualquier cambio en routes/whatsapp.py, payments.py, o tickets.py puede romper el flujo de ventas. Verificar que el status lifecycle se respeta y que los webhooks de Stripe disparan correctamente.
