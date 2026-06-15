# MIA Watchdog — Corrections Log

Plano append-only de auto-correcciones duras. Formato: 1 línea por corrección.

Columnas: `slot | detectado-en | métrica | valor-reportado | valor-real | slots-afectados | causa-raíz`

---

| slot | detected-in | metric | reported | actual | slots-affected | root-cause |
|---|---|---|---|---|---|---|
| #46 | #46 (22:04Z 14-jun) | misrouting-h (SHA-marker secundario) | usaba `5e7b78c @ 2026-06-11T22:04Z` (inferido erróneo) → +24h inflado | timestamp real `5e7b78c @ 2026-06-12T22:05:58Z` (commit watchdog #23); marker correcto del misrouting siempre fue `64ba80b @ 2026-06-09T04:05:32Z` (ALERT) | #38→#45 (8 slots) | reusé SHA narrativo sin re-derivar timestamp; arrastrado por 9 slots |
| #47 | #47 (00:04Z 15-jun) | `mia-alan-bridge.updated_at` | reportado `2026-06-12T04:58:06Z` desde #28 hasta #46 (~19 slots) | hoy el search API devuelve `2026-05-28T00:02:04Z` (15 días MÁS VIEJO) | #28→#46 (~19 slots) si el valor de hoy es el verdadero; #47 mismo si es flicker de cache | regression imposible naturalmente: o (a) cache shard sirviendo datos inconsistentes, o (b) los valores previos eran un campo derivado distinto (e.g. último issue/comment) y el correcto siempre fue el de hoy. Sin acceso per-repo (scope MCP) no puedo discriminar. Reporto ambas lecturas y dejo cicatriz. |
| #47 (resolución) | #48 (02:04Z 15-jun) | `mia-alan-bridge.updated_at` | "regresión clase A" del #47 (sospecha de cache flicker o cambio externo) | NO ERA REGRESIÓN. Fue field-swap del watchdog: durante #28→#46 leí `pushed_at=2026-06-12T04:58:06Z` y lo etiqueté `updated_at`. Hoy API devuelve EXACTAMENTE lo mismo en ambos campos: `pushed_at=2026-06-12T04:58:06Z` (idéntico a mis 19 reports previos) y `updated_at=2026-05-28T00:02:04Z` (el campo correcto que vi en #47 por primera vez). Hipótesis (b) del #47 CONFIRMADA. (a) flicker y (c) force-push descartadas. | #28→#46 etiqueta corregida en este registro; los valores numéricos de "silencio del repo" fueron correctos cuando se interpretan como silencio de `pushed_at` | confusión `pushed_at` vs `updated_at` (semántica distinta en search API: `pushed_at`=último git push a cualquier rama, `updated_at`=último cambio de metadata del repo). Auto-corrección dura #2 del watchdog. Prevención estructural en #48: la tabla del cluster MIA reporta ambos campos separadamente. |

## Regla emergente

Cualquier referencia a SHA o timestamp en un report del watchdog DEBE re-derivarse desde la fuente primaria al inicio del run que lo cite (Invariante #6 candidato — auto-auditoría sistemática). Si la fuente es `mcp__github__search_repositories.updated_at`, marcar explícitamente esa dependencia para que próximas regresiones se detecten al instante.

## Regla emergente #2 (a partir del #48)

Cualquier métrica derivada del search API que tenga `pushed_at` Y `updated_at` debe reportarse con el nombre del campo explícitamente en la tabla. El "silencio del repo" por sí solo es ambiguo: el lector debe poder distinguir si la cifra cuenta desde el último push de código (`pushed_at`) o desde el último cambio de metadata (`updated_at`). Prevención estructural anti field-swap.
