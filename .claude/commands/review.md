Revisa todos los archivos modificados en esta rama vs main. Ejecuta `git diff main...HEAD` para ver los cambios.

Para cada archivo modificado, verifica:

## Security
- [ ] Inputs de usuario usados en paths están validados contra path traversal (`..`, `/`, `\`)
- [ ] Endpoints protegidos tienen `_validate_auth()`
- [ ] No hay secrets hardcoded (buscar patterns: `sk-`, `api_key="`, `password=`, `Bearer ` con valor literal)
- [ ] Uploads validan MIME type y tamaño

## Bugs lógicos
- [ ] No hay strings donde debería haber llamadas (`"now()"`, `"uuid4()"`, etc.)
- [ ] Endpoints de APIs externas son correctos (OpenAI: `/v1/chat/completions`, Telnyx: `/v2/`)
- [ ] Payloads de APIs usan las keys correctas (`messages` no `input`, `max_tokens` no `max_output_tokens`)
- [ ] Respuestas de APIs se parsean correctamente

## Supabase
- [ ] Usa `sb` de `deps.py`, no crea clientes nuevos
- [ ] Timestamps con `datetime.now(timezone.utc).isoformat()`
- [ ] Manejo seguro de data vacía: `(r.data or [None])[0]`
- [ ] Updates usan valores Python reales, no strings SQL

## Calidad
- [ ] Usa `logger.*` no `print()`
- [ ] Errores se loggean antes de raise
- [ ] try/except en llamadas externas (DB, API, red)
- [ ] Mensajes de error truncados: `str(exc)[:200]`

Para cada hallazgo reporta:
| Archivo | Línea | Severidad | Problema | Fix sugerido |

Si todo está bien, confirma con "LGTM - No issues found."
