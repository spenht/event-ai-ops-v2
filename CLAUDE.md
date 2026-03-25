# Event AI Ops v2

FastAPI backend for event management with AI-powered WhatsApp automation, voice calls, payments, and commission tracking.

## Stack

- **Runtime:** Python 3.11 + FastAPI + Uvicorn
- **Database:** Supabase (PostgreSQL + Storage)
- **APIs externas:** OpenAI, Telnyx, Twilio, Stripe, ElevenLabs, Meta Conversions
- **Deploy:** Fly.io

## Estructura

```
app/
├── main.py              # FastAPI app + CORS + routers
├── settings.py          # Frozen dataclass, all config from env vars
├── deps.py              # Shared Supabase client (sb)
├── routes/              # API endpoints (22 modules)
└── services/            # Business logic (17 modules)
```

## Reglas de código

### Timestamps
- SIEMPRE usar `datetime.now(timezone.utc).isoformat()` para fechas
- NUNCA usar `"now()"` como string — se guarda literal en la DB
- NUNCA usar `datetime.utcnow()` (deprecated, no tiene timezone)
- Import: `from datetime import datetime, timezone`

### Supabase
- Usar el cliente compartido: `from ..deps import sb`
- NUNCA crear instancias nuevas con `create_client()` (excepto en media.py upload)
- Manejo seguro de datos vacíos: `(result.data or [None])[0]` o `(result.data or [])`
- Updates deben usar valores reales de Python, no strings de funciones SQL

### Autenticación
- Toda ruta protegida DEBE tener `_validate_auth(request)`
- Orden de validación: cron_token → spartans_key (campaign) → spartans_key (global)
- Headers siempre con `.get().strip()`

### APIs externas
- Usar `httpx.AsyncClient(timeout=12.0)` para llamadas async
- OpenAI endpoint: `https://api.openai.com/v1/chat/completions` (NUNCA `/v1/responses`)
- Payload OpenAI: `messages` (no `input`), `max_tokens` (no `max_output_tokens`)
- Siempre verificar `resp.status_code >= 400` antes de parsear
- Truncar errores en logs: `str(exc)[:200]`

### Seguridad
- Todo input de usuario en paths DEBE validarse contra path traversal (`..`, `/`, `\`)
- Buckets de storage en allowlist, nunca hardcoded inline
- NUNCA hardcodear secrets — todo viene de `settings.*` (env vars)
- Validar MIME types y tamaños de archivo en uploads

### Logging
- Usar `logger = logging.getLogger(__name__)` en cada módulo
- Formato estructurado: `logger.info("event_name key=%s", value)`
- NUNCA usar `print()` — siempre `logger.*`
- Truncar detalles de error a 200-300 chars

### Settings & Config
- Toda config en `app/settings.py` como frozen dataclass
- Patrón fallback: campaign DB → settings global → default sensible
- API keys por campaña: `campaign.get("openai_api_key") or settings.openai_api_key`
- JSON fields de DB: siempre parsear con `isinstance` check

### Errores
- `try/except` en toda llamada externa (DB, API, red)
- Loggear ANTES de hacer raise
- 400 para validación, 401/403 para auth, 500 para errores internos
- Nunca exponer stack traces al cliente

## Comandos útiles

```bash
# Dev server
uvicorn app.main:app --reload --port 8000

# Deploy
fly deploy
```
