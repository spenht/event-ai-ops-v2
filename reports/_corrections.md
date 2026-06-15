# MIA Watchdog — Corrections Log

Plano append-only de auto-correcciones duras. Formato: 1 línea por corrección.

Columnas: `slot | detectado-en | métrica | valor-reportado | valor-real | slots-afectados | causa-raíz`

---

| slot | detected-in | metric | reported | actual | slots-affected | root-cause |
|---|---|---|---|---|---|---|
| #46 | #46 (22:04Z 14-jun) | misrouting-h (SHA-marker secundario) | usaba `5e7b78c @ 2026-06-11T22:04Z` (inferido erróneo) → +24h inflado | timestamp real `5e7b78c @ 2026-06-12T22:05:58Z` (commit watchdog #23); marker correcto del misrouting siempre fue `64ba80b @ 2026-06-09T04:05:32Z` (ALERT) | #38→#45 (8 slots) | reusé SHA narrativo sin re-derivar timestamp; arrastrado por 9 slots |
| #47 | #47 (00:04Z 15-jun) | `mia-alan-bridge.updated_at` | reportado `2026-06-12T04:58:06Z` desde #28 hasta #46 (~19 slots) | hoy el search API devuelve `2026-05-28T00:02:04Z` (15 días MÁS VIEJO) | #28→#46 (~19 slots) si el valor de hoy es el verdadero; #47 mismo si es flicker de cache | regression imposible naturalmente: o (a) cache shard sirviendo datos inconsistentes, o (b) los valores previos eran un campo derivado distinto (e.g. último issue/comment) y el correcto siempre fue el de hoy. Sin acceso per-repo (scope MCP) no puedo discriminar. Reporto ambas lecturas y dejo cicatriz. |

## Regla emergente

Cualquier referencia a SHA o timestamp en un report del watchdog DEBE re-derivarse desde la fuente primaria al inicio del run que lo cite (Invariante #6 candidato — auto-auditoría sistemática). Si la fuente es `mcp__github__search_repositories.updated_at`, marcar explícitamente esa dependencia para que próximas regresiones se detecten al instante.
