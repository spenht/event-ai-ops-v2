---
description: Supabase client and database interaction patterns
globs: app/**/*.py
---

# Supabase Patterns

## Client Usage
- Use the shared client: `from ..deps import sb`
- NEVER create new clients with `create_client()` in route/service code
- Exception: media.py upload uses service_role_key for storage (documented)

## Timestamps
- ALWAYS: `datetime.now(timezone.utc).isoformat()`
- NEVER: `"now()"` as a string — this saves the literal text "now()" in the database
- NEVER: `datetime.utcnow()` — deprecated and timezone-naive
- Import: `from datetime import datetime, timezone`

## Query Patterns
```python
# Read - safe empty data handling
r = sb.table("table").select("*").eq("id", id).limit(1).execute()
row = (r.data or [None])[0]

# Update - real Python values, not SQL strings
sb.table("table").update({
    "status": "approved",
    "updated_at": datetime.now(timezone.utc).isoformat()
}).eq("id", id).execute()

# Insert
r = sb.table("table").insert(row_dict).execute()
created = (r.data or [None])[0]
```

## Error Handling
- Always wrap DB calls in try/except
- Log errors before re-raising
- Return sensible defaults for non-critical reads: `campaign = None`
- Use `.get("key")` for dict access, never `["key"]` on DB results

## JSON Fields
- Campaign columns like `number_pool_config`, `ticket_config` may be JSON strings
- Always check with `isinstance(value, str)` before parsing
- Parse safely: `json.loads(value)` inside try/except
