---
name: Known Issues
description: Active bugs, security risks, and technical debt identified in codebase audit
type: project
---

# Known Issues

## CRITICAL — Security

### Webhooks sin verificación de firma
- **telnyx_webhooks.py** — Acepta cualquier JSON, no verifica X-Telnyx-Signature
- **stripe_connect.py:214-234** — Si no hay webhook secret, acepta payload sin verificar
- **whatsapp.py:677** — No verifica X-Twilio-Signature
- **Riesgo:** Atacante puede forjar eventos de pago, llamadas, o mensajes

### Bearer token sin validar (media.py:29)
- Acepta CUALQUIER string no vacío como Bearer token
- **Fix:** Validar JWT con Supabase

### CORS wildcard + credentials (main.py:36-42)
- `allow_origins=["*"]` con `allow_credentials=True`
- **Riesgo:** Cualquier sitio puede hacer requests autenticados

### Service role key en endpoint público (media.py:67)
- `create_client(url, service_role_key)` tiene acceso total a la DB
- **Fix:** Usar RLS con anon key

## HIGH — Bugs

### Unsafe .data[0] sin null check
- **commission_engine.py:** líneas 29, 46, 65, 74, 239
- **traffic_sources.py:** líneas 97, 114, 163, 182
- **checkin.py:** línea 417
- **commissions.py:** línea 129
- **Riesgo:** IndexError en producción con data vacía
- **Fix:** Usar `(r.data or [None])[0]`

## MEDIUM — Calidad

### except: pass silenciosos (sin logging)
- **google_sheets.py:** 7+ instancias
- **number_pool.py:** 10+ instancias
- **tickets.py:** 6 instancias
- **commission_engine.py:** 4 instancias
- **Fix:** Agregar `logger.warning()` mínimo

### Rate limiting en memoria (lead_capture.py:233)
- `_rate_store` dict no funciona con múltiples instancias Fly.io
- **Fix:** Usar Redis o Supabase para rate limiting distribuido

### WebSocket JSON parse sin try/catch (call_media_ws.py:164)
- `json.loads(raw)` puede crashear el handler
- **Fix:** Wrap en try/except

## LOW — Best Practices

### Packages no pinned (requirements.txt)
- `gspread>=6.0`, `google-auth>=2.0` usan `>=` en vez de `==`

### Docker image no pinned a digest
- `FROM python:3.11-slim` sin SHA digest
