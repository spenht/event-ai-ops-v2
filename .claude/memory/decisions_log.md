---
name: Decisions Log
description: Technical and business decisions made during development with rationale
type: project
---

# Decisions Log

## 2026-03-25: Corregir "now()" strings en Supabase updates
- **Decisión:** Reemplazar `"now()"` string literal con `datetime.now(timezone.utc).isoformat()` en todo el codebase
- **Why:** Supabase client de Python no interpreta funciones SQL en valores. El string "now()" se guardaba literal en la DB.
- **Archivos:** commissions.py, landing_pages.py
- **Regla:** Documentado en CLAUDE.md y .claude/rules/supabase.md

## 2026-03-25: Corregir endpoint OpenAI /v1/responses → /v1/chat/completions
- **Decisión:** Migrar todos los calls a OpenAI al endpoint correcto
- **Why:** `/v1/responses` no existe en OpenAI. Payload keys también eran incorrectas.
- **Archivos:** openai_chat.py, post_call_processor.py, spartan_dashboard.py
- **Regla:** Documentado en .claude/rules/external-apis.md

## 2026-03-25: Agregar validación path traversal en storage endpoints
- **Decisión:** Validar campaign_id, path, y bucket contra `..`, `/`, `\` y usar allowlist de buckets
- **Why:** Sin validación, un atacante podía acceder a cualquier archivo en Supabase Storage
- **Archivos:** media.py, landing_pages.py
- **Regla:** Documentado en .claude/rules/security.md

## 2026-03-25: Implementar GitHub Actions CI para PRs a main
- **Decisión:** 3 jobs paralelos: syntax check, anti-pattern scanner, security scanner
- **Why:** Prevenir que los mismos bugs lleguen a producción
- **Archivo:** .github/workflows/pr-checks.yml

## 2026-03-25: Crear estructura .claude/ con rules, commands, memory, agents
- **Decisión:** Todo el conocimiento del proyecto vive en el repo, versionado con git
- **Why:** El equipo obtiene las reglas con `git pull`. No necesita marketplace ni repo separado.

## Decisión pendiente: Separar staging vs production
- **Plan:** Supabase actual = staging, crear nuevo proyecto = production
- **Bloqueado por:** Necesita admin access para branch protection en GitHub
