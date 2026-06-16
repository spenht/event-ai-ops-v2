# 🚨 ALERT — MIA Watchdog routed to WRONG repo

**Timestamp (UTC):** 2026-06-09T04:04:41Z
**Run:** watchdog cron (Anthropic cloud, Opus 4.7)
**Severity:** HIGH — protocol cannot execute, no auto-fix possible

## Resumen (Ley #15: honesto, sin inventar)

El prompt MIA WATCHDOG aterrizó en el repo `spenht/event-ai-ops-v2`.
Este repo NO es el de los bots de trading. Es el backend FastAPI de
gestión de eventos (WhatsApp/Twilio/Stripe/Supabase). Estructura
confirmada:

```
event-ai-ops-v2/
├── app/{routes,services,prompts}/   # FastAPI event mgmt
├── sql/
├── tests/
├── .claude/
│   ├── agents/{pre-deploy, security-reviewer}.md
│   ├── memory/{business_model, customer_journey, integrations, ...}.md
│   └── rules/{security, supabase, external-apis}.md
├── CLAUDE.md                         # "Event AI Ops v2 — FastAPI backend"
├── MASTER_CONTEXT.md
└── README.md
```

**Archivos esperados por el protocolo MIA — TODOS AUSENTES:**
- ❌ `memory/MIA_LAWS.md`
- ❌ `memory/PROTOCOLO_MIA.md`
- ❌ `memory/SYSTEM_WORKING_may28.md`
- ❌ `status/mia_status_latest.json`
- ❌ `reports/watchdog_*.md` (este directorio no existía; lo creé para depositar este ALERT)
- ❌ `actions/queue/`

**Búsqueda exhaustiva ejecutada:**
```
find /home/user/event-ai-ops-v2 -iname "MIA*" -o -iname "mia*"
→ (sin resultados)
find / -maxdepth 3 -type d -name memory
→ /sys/bus/memory  (no es el correcto)
```

## Por qué NO inventé las 9 preguntas

Las 9 preguntas (Ley #23) exigen datos reales:
- "Wallet $X (vs ayer)" — no tengo acceso a wallet ni nada parecido aquí
- "Iters discovery progress" — no hay iteradores en este repo
- "Bots fired last 24h" — no hay bots, este es un backend de tickets/eventos
- "ES/NQ/MES paper, SPXW, BR-stocks, 4 snipers" — ninguno existe en este árbol

Aplicar la Ley #15 ("Honestidad > hype. Si no fixeaste algo, di NO. No
inventes.") prohíbe fabricar métricas. Y la Ley #21 ("¿esto mueve la
aguja a real money esta semana?") confirma que un report MIA falso
en un repo de eventos NO mueve aguja — la confunde.

## Hipótesis del origen del mismatch

1. **GitHub Action / cron mal configurado:** el job `mia-watchdog`
   probablemente apunta a `spenht/event-ai-ops-v2` como `repository`
   cuando debería apuntar a un repo tipo `spenht/mia-*` o
   `spenht/mia-watchdog-state`.
2. **Sesión de Claude Code on the web disparada en el repo equivocado:**
   el trigger en code.claude.com tiene `event-ai-ops-v2` clavado.
3. **Scope MCP restringido:** la propia sesión confirma
   "Repository Scope: spenht/event-ai-ops-v2" — incluso si quisiera
   `git clone` el repo correcto vía gh, no podría (gh no está
   disponible aquí; GitHub solo vía MCP y bloqueado a este repo).

## Acciones que NO ejecuté (y por qué)

- ❌ **No creé** `memory/MIA_LAWS.md` falso ni stubs vacíos — habría
  contaminado este repo con archivos sin sentido para el equipo del
  proyecto de eventos.
- ❌ **No moví/clonée** datos de otro repo — sin red ni scope MCP a
  otros repos, imposible.
- ❌ **No escribí** un report con métricas fabricadas — viola Ley #15.

## Lo que Spencer debe decidir (acción humana requerida)

**Opción A (más probable correcta):** Reconfigurar el trigger del
watchdog en code.claude.com para que apunte al repo correcto. Buscar
en code.claude.com → environments → buscar "mia" / "watchdog" /
"beast" / "iterator" → confirmar el `repository` field.

**Opción B:** Si el repo de estado MIA aún no existe como repo Git
remoto, crearlo (`spenht/mia-watchdog-state`) con el bootstrap:
```
memory/MIA_LAWS.md
memory/PROTOCOLO_MIA.md
memory/SYSTEM_WORKING_may28.md
status/.gitkeep
reports/.gitkeep
actions/queue/.gitkeep
```
y reapuntar el cron ahí.

**Opción C:** Si la intención SÍ era que este repo (`event-ai-ops-v2`)
hospede al watchdog MIA (raro, pero posible si Spencer quiere
co-ubicar), confirmarlo y commitearé los archivos de bootstrap aquí
en el próximo run.

## Próximo run

Sin acción humana este ALERT se va a regenerar cada 2h. Sugiero pausar
el cron `mia-watchdog` en code.claude.com hasta resolver. Mientras
tanto, no escribiré reports falsos.

## Snapshot honesto del repo donde caí

- Branch: `main` (HEAD estaba detached, lo attacho antes de commit)
- Último commit: `9d154dd fix(calls): create_call_record envía lead_id=NULL explícito (#10)`
- Working tree: clean
- Stack: FastAPI + Supabase + Twilio + Telnyx + Stripe + OpenAI
- Propósito: backend de eventos con WhatsApp automation, voice calls,
  payments, commission tracking. Despliega en Fly.io.
- Sin relación con trading bots.

— MIA WATCHDOG (Opus 4.7, Anthropic cloud)
