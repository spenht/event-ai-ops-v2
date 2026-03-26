---
name: Key Entities
description: Core domain entities - campaigns, leads, spartans, commissions, tickets and their relationships
type: project
---

# Entidades del Dominio

## Campaigns (contenedor principal)
- Multi-tenant: cada campaña tiene credenciales propias (Twilio, Telnyx, Stripe, OpenAI)
- Contiene: event_name, date, location, speakers, vip_price, AI config
- Campos JSON: `number_pool_config`, `ticket_config`, `stripe_price_ids`, `call_retry_config`
- Siempre parsear JSON fields con `isinstance(value, str)` check

## Leads (persona/prospecto)
- Core: name, email, phone, campaign_id, status, tier_interest, payment_status
- Phone: México tiene variantes +52 vs +521 (normalizar siempre)
- Status lifecycle: NEW → GENERAL_CONFIRMED → VIP_INTERESTED → VIP_LINK_SENT → PAID
- Deduplicación por MessageSid en WhatsApp inbound

## Spartans (agentes de ventas humanos)
- Hacen llamadas outbound a leads via Telnyx
- Ganan comisiones por conversiones VIP
- Dashboard con: cola de leads, métricas, leaderboard, historial
- AI Coach analiza grabaciones y da feedback
- Autenticación via `x-spartans-key` header (por campaña o global)

## Commissions (comisiones)
- Atribución: se busca última llamada de Spartan al lead
- Tipos: fixed ($ fijo) o percentage (% de venta)
- Escalación por volumen: más ventas → mayor comisión
- Status: pending → approved → paid
- Timestamps: approved_at, paid_at (UTC ISO format)

## Tickets (entradas al evento)
- Generados como PNG con QR code
- Seguridad: acceso via token único
- Storage: Supabase Storage o filesystem
- Custom design por campaña (AI genera CSS)

## Orgs (organizaciones)
- Dueños de eventos
- stripe_account_id para Stripe Connect
- Miembros con roles

## Relaciones
```
orgs (1) → (∞) campaigns → (∞) leads → (∞) touchpoints
                                      → (1) tickets
                                      → (∞) call_records
                          → (∞) commission_configs → tiers
                          → (∞) traffic_sources
                          → (∞) forms
                          → (∞) broadcasts
```

**How to apply:** Al modificar queries a Supabase, respetar estas relaciones. Campaign es siempre el scope principal. Lead es el centro del flujo de negocio.
