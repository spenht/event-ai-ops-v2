Checklist de pre-deploy. Verifica CADA punto y reporta PASS/FAIL con evidencia.

## Critical — Bloquean deploy

1. **No "now()" strings**
   `grep -r '"now()"' app/` — debe retornar vacío

2. **No path traversal**
   Verificar que todo endpoint que reciba IDs de usuario y los use en paths tenga validación contra `..`, `/`, `\`

3. **Endpoints de APIs correctos**
   - OpenAI: `/v1/chat/completions` (no `/v1/responses`)
   - Telnyx: `/v2/` (no `/v1/`)

4. **No secrets en código**
   `grep -rE "(sk-[a-zA-Z0-9]{20}|api_key\s*=\s*['\"][^'\"]+['\"])" app/` — debe retornar vacío

5. **Auth en endpoints protegidos**
   Todo POST/PUT/DELETE en routes debe tener `_validate_auth()` o justificación documentada

## High — Deben corregirse antes de deploy

6. **Timestamps UTC**
   `grep -r "datetime.now()" app/` — todos deben incluir `timezone.utc`
   `grep -r "utcnow()" app/` — no debe existir

7. **Supabase safe access**
   Verificar que no haya `.data[0]` sin manejo de None

8. **No print() statements**
   `grep -r "print(" app/` — debe retornar vacío (usar logger)

## Medium — Deseables

9. **Error truncation**
   Verificar que `str(exc)` en logs esté truncado a [:200] o [:300]

10. **File size limits**
    Upload endpoints deben tener MAX_FILE_SIZE definido

## Formato de resultado

```
PRE-DEPLOY CHECK — {fecha}
═══════════════════════════
1. No "now()" strings     [PASS ✓] / [FAIL ✗] — {detalle}
2. No path traversal       [PASS ✓] / [FAIL ✗] — {detalle}
...

RESULTADO: READY / BLOCKED ({N} critical failures)
```

Si algún check CRITICAL falla: resultado es BLOCKED, NO proceder con deploy.
