---
description: Rules for external API integrations (OpenAI, Telnyx, Stripe, etc.)
globs: app/services/*.py, app/routes/*.py
---

# External API Rules

## HTTP Client
- Use `httpx.AsyncClient(timeout=12.0)` for all async HTTP calls
- Always use context manager: `async with httpx.AsyncClient() as client:`
- Check `resp.status_code >= 400` before parsing response
- Log errors with truncated body: `resp.text[:1200]`

## OpenAI
- Endpoint: `https://api.openai.com/v1/chat/completions`
- NEVER use `/v1/responses` — that endpoint does not exist
- Payload key: `"messages"` (not `"input"`)
- Token limit key: `"max_tokens"` (not `"max_output_tokens"`)
- Response parsing: `data["choices"][0]["message"]["content"]`
- API key fallback: `campaign.get("openai_api_key") or settings.openai_api_key`

## Telnyx
- Base URL: `https://api.telnyx.com/v2`
- Auth header: `Bearer {settings.telnyx_api_key}`
- Webhook state via base64-encoded client_state JSON

## Stripe
- Use `stripe` SDK, not raw HTTP
- Always set `stripe.api_key` before calls
- Platform vs direct: check if using Connect (platform key) or direct (secret key)

## Error Handling
- Always wrap API calls in try/except
- Log with: `logger.exception("service_error err=%s", str(exc)[:300])`
- Return `None` on failure for non-critical calls
- Raise `HTTPException` for user-facing failures
