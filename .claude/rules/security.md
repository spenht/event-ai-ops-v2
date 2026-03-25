---
description: Security rules for route handlers and services
globs: app/routes/*.py, app/services/*.py
---

# Security Rules

## Path Traversal
- Every user-supplied parameter used in file paths or storage paths MUST be validated:
  ```python
  if ".." in user_input or "/" in user_input or "\\" in user_input:
      raise HTTPException(status_code=400, detail="Invalid input")
  ```
- This applies to: campaign_id, file names, bucket names, any ID used in storage paths

## Storage Buckets
- Bucket names MUST be validated against an allowlist constant:
  ```python
  ALLOWED_BUCKETS = {"whatsapp", "assets", "media"}
  ```
- Never pass user input directly as a bucket name

## Authentication
- Every route that modifies data MUST call `_validate_auth(request)`
- Auth check order: cron_token → campaign spartans_key → global spartans_key
- Never trust client headers without `.strip()`
- Use 401 for missing auth, 403 for invalid auth

## Secrets
- ALL secrets come from environment variables via `settings.*`
- NEVER hardcode API keys, tokens, passwords, or connection strings
- NEVER log full API keys — truncate or mask them
- Campaign-specific keys from DB override global settings

## Input Validation
- Validate MIME types before processing uploads
- Enforce file size limits before reading full content
- Use Pydantic BaseModel for request body validation
- Sanitize any input used in database queries or external API calls
