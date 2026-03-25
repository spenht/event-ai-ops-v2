Ejecuta un audit completo del codebase en `app/`. Busca problemas en estas categorías:

## 1. Security — CRITICAL
```bash
# Path traversal: inputs de usuario en paths sin validar
# Buscar en routes que reciban IDs y los usen en storage/file paths

# Secrets hardcoded
grep -r "sk-" "api_key=" "password=" "Bearer " con valores literales

# Endpoints sin auth
# Routes con @router.post/put/delete sin _validate_auth()
```

## 2. Bugs conocidos — HIGH
```bash
# String literals donde deberían ser funciones
grep -r '"now()"' app/
grep -r "'now()'" app/

# Endpoints incorrectos de APIs externas
# OpenAI debe ser /v1/chat/completions
# Telnyx debe ser /v2/

# f-strings malformados
# Payloads con keys incorrectas (input vs messages, max_output_tokens vs max_tokens)
```

## 3. Timestamps — MEDIUM
```bash
# Verificar que todos los timestamps sean UTC-aware
grep -r "datetime.now()" app/  # debe tener timezone.utc
grep -r "utcnow()" app/        # deprecated, no debe existir
```

## 4. Supabase — MEDIUM
```bash
# Clientes creados fuera de deps.py
grep -r "create_client" app/    # solo debe estar en deps.py y media.py

# Data sin manejo seguro
# Buscar .data[0] sin or [None]
```

## 5. Logging — LOW
```bash
# print() statements
grep -r "print(" app/

# Errores sin truncar
# Exception messages expuestas al cliente sin truncar
```

## Formato de reporte

Para cada hallazgo:
| # | Archivo | Línea | Categoría | Severidad | Descripción | Fix |

Al final, un resumen:
- Total hallazgos por severidad
- Top 3 archivos con más problemas
- Acción recomendada inmediata
