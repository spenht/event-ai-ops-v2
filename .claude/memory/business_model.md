---
name: Business Model
description: SaaS platform for event ticket sales via WhatsApp AI - core business model and revenue streams
type: project
---

# Business Model — Event AI Ops v2

**Qué es:** Plataforma SaaS WhatsApp-first para venta de tickets de eventos con AI, voice calls, y red de afiliados.

**Mercado:** Eventos en México/Latinoamérica, audiencia hispanohablante.

**Modelo de ingresos:**
- SaaS platform fee via Stripe Connect (porcentaje por transacción)
- Los organizadores de eventos (orgs) venden tickets a través de la plataforma
- Cada org tiene su propia cuenta Stripe Connect

**Propuesta de valor:**
1. Captura leads desde ads/landing pages
2. AI conversacional en WhatsApp (español, tono natural, personaje "Ana")
3. Upsell automatizado de General → VIP con follow-ups estratégicos
4. Checkout via Stripe con entrega instantánea de ticket QR
5. Red de "Spartans" (agentes de ventas) con comisiones y leaderboard

**Multi-tenancy:**
- Cada campaña es un contenedor aislado con sus propias credenciales (Twilio, Telnyx, Stripe, OpenAI)
- Fallback a credenciales globales si la campaña no tiene propias
- Patrón: `campaign.get("key") or settings.key`

**Why:** El negocio depende de conversión rápida via WhatsApp. Cada decisión técnica debe priorizar: velocidad de respuesta al lead, confiabilidad de pagos, y entrega de tickets.

**How to apply:** Al proponer features o fixes, priorizar lo que impacta directamente la conversión (WhatsApp response time, payment flow, ticket delivery). Los Spartans son el canal de ventas humano — su dashboard y comisiones deben ser confiables.
