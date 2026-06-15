# MIA Watchdog — Corrections Log

Plano append-only de auto-correcciones duras. Formato: 1 línea por corrección.

Columnas: `slot | detectado-en | métrica | valor-reportado | valor-real | slots-afectados | causa-raíz`

---

| slot | detected-in | metric | reported | actual | slots-affected | root-cause |
|---|---|---|---|---|---|---|
| #46 | #46 (22:04Z 14-jun) | misrouting-h (SHA-marker secundario) | usaba `5e7b78c @ 2026-06-11T22:04Z` (inferido erróneo) → +24h inflado | timestamp real `5e7b78c @ 2026-06-12T22:05:58Z` (commit watchdog #23); marker correcto del misrouting siempre fue `64ba80b @ 2026-06-09T04:05:32Z` (ALERT) | #38→#45 (8 slots) | reusé SHA narrativo sin re-derivar timestamp; arrastrado por 9 slots |
| #47 | #47 (00:04Z 15-jun) | `mia-alan-bridge.updated_at` | reportado `2026-06-12T04:58:06Z` desde #28 hasta #46 (~19 slots) | hoy el search API devuelve `2026-05-28T00:02:04Z` (15 días MÁS VIEJO) | #28→#46 (~19 slots) si el valor de hoy es el verdadero; #47 mismo si es flicker de cache | regression imposible naturalmente: o (a) cache shard sirviendo datos inconsistentes, o (b) los valores previos eran un campo derivado distinto (e.g. último issue/comment) y el correcto siempre fue el de hoy. Sin acceso per-repo (scope MCP) no puedo discriminar. Reporto ambas lecturas y dejo cicatriz. |
| #47 (resolución) | #48 (02:04Z 15-jun) | `mia-alan-bridge.updated_at` | "regresión clase A" del #47 (sospecha de cache flicker o cambio externo) | NO ERA REGRESIÓN. Fue field-swap del watchdog: durante #28→#46 leí `pushed_at=2026-06-12T04:58:06Z` y lo etiqueté `updated_at`. Hoy API devuelve EXACTAMENTE lo mismo en ambos campos: `pushed_at=2026-06-12T04:58:06Z` (idéntico a mis 19 reports previos) y `updated_at=2026-05-28T00:02:04Z` (el campo correcto que vi en #47 por primera vez). Hipótesis (b) del #47 CONFIRMADA. (a) flicker y (c) force-push descartadas. | #28→#46 etiqueta corregida en este registro; los valores numéricos de "silencio del repo" fueron correctos cuando se interpretan como silencio de `pushed_at` | confusión `pushed_at` vs `updated_at` (semántica distinta en search API: `pushed_at`=último git push a cualquier rama, `updated_at`=último cambio de metadata del repo). Auto-corrección dura #2 del watchdog. Prevención estructural en #48: la tabla del cluster MIA reporta ambos campos separadamente. |
| #52 | #52 (10:04Z 15-jun) | planificación de acción (pivot a GitHub Issue) | el plan del #51 para #52 decía "abro 1 GitHub Issue acá con resumen ejecutivo del ALERT + 51 reports si `_inbox/` llega a 0/6"; el #51 asumió que no había Issue abierta previa porque ningún report desde #44 leyó `list_issues` | **Issue #11 ya existe ABIERTA desde 2026-06-13T14:07:45Z (= run #31)** con cuerpo equivalente al pivot planificado (44h sin respuesta de Spencer al momento del #52). El #51 olvidó que el pivot ya se hizo. | #44→#51 (8 slots) planificación con suposición falsa "no hay pivot a Issue consumido"; ningún slot tomó la acción concreta de re-crear Issue (porque el plan estaba diferido), por lo que el olvido no produjo duplicación — solo planificación equivocada repetida | el invariante #6 (auto-auditoría) cubría datos de cluster MIA + behind-by-N + ALERT + Spencer-most-recent, pero NO cubría estado-issues del repo de routing. Punto ciego de memoria-larga: 20 slots × 2h = 40h de planificación sobre estado obsoleto. **Auto-corrección dura #3** del watchdog. Prevención estructural en #52: checklist del invariante #6 extendido con step `list_issues state=OPEN del repo de routing al inicio del run` aplicable desde #53. **Decisión disciplinada**: NO crear Issue duplicada, NO comentar Issue #11 (Spencer ignoró 44h, 2º grito sería ruido) — aplicar guidance "be frugal about posting replies on GitHub". |

## Regla emergente

Cualquier referencia a SHA o timestamp en un report del watchdog DEBE re-derivarse desde la fuente primaria al inicio del run que lo cite (Invariante #6 candidato — auto-auditoría sistemática). Si la fuente es `mcp__github__search_repositories.updated_at`, marcar explícitamente esa dependencia para que próximas regresiones se detecten al instante.

## Regla emergente #2 (a partir del #48)

Cualquier métrica derivada del search API que tenga `pushed_at` Y `updated_at` debe reportarse con el nombre del campo explícitamente en la tabla. El "silencio del repo" por sí solo es ambiguo: el lector debe poder distinguir si la cifra cuenta desde el último push de código (`pushed_at`) o desde el último cambio de metadata (`updated_at`). Prevención estructural anti field-swap.

---

| #54 | #54 (14:04Z 15-jun) | behind-by-N | reportado #46→#53 como "+1/slot a 13 muestras, +2 offset estructural permanente, post-push N en #N (e.g. 53 post-push en #53)" | **real**: `git rev-list --count 61a2b93 (=#53) = 50`, `fd0303e (=#52) = 49`, `86e9403 (=#51) = 48`. Fórmula correcta `depth(run_N).origin/main = N - 3`. Root de origin/main = `324227e @ 2026-06-09T10:05:05Z = watchdog #4`. **3 commits pre-#4 (#1=ALERT, #2, #3) nunca llegaron a origin/main** — no 2 como #53 infirió. El "+2 offset" era narrative-drag construido sobre asumir que el contador raw debía igualar el run-number; en realidad el raw siempre fue depth correcto y el reported lo infló +3 retroactivamente. | #46→#53 (8 slots) reportaron números inflados (50→53 en #53, 49→52 en #52, etc.). Sin impacto operacional para Spencer. Cuestión de calidad de método. | el invariante #6 (auto-auditoría) cubría hasta #53 estado externo (`list_issues` capa C3) pero NO incluía verificación aritmética del propio contador contra fuente primaria. Hueco: lectura literal de `wc -l` ajustada con offset narrativo sin nunca correr `git rev-list --count` para validar la fórmula. **Auto-corrección dura #4** del watchdog. Tipología nueva: `narrative-drag-numeric`. Prevención estructural en #54: **capa C4 introducida** = verificación aritmética de contadores numéricos del report contra fuente primaria en T mismo (no T+1). **Decisión disciplinada**: documentar la fórmula `depth = N - 3` y aplicar a contadores futuros. |

## Regla emergente #3 (a partir del #54)

Cualquier contador numérico que aparezca en el report con fórmula derivada (offset, multiplicador, suma sobre fuente primaria) DEBE re-verificarse contra fuente primaria en el slot que lo cita. Comandos canonical para behind-by-N: `git rev-list --count HEAD`, `git rev-list --count <SHA>` por commit. **Política operacional meta emergente**: `fuente-primaria > memoria-del-watchdog`. Promovible a invariante #8 en #55.

## Tipología de auto-correcciones (a partir del #54)

| tipo | slot ejemplo | descripción |
|---|---|---|
| `narrative-drag` | #46 | reuso de SHA/timestamp narrativo sin re-derivar contra fuente primaria |
| `field-swap` | #47 | confusión entre campos semánticamente distintos del mismo objeto API (`pushed_at` vs `updated_at`) |
| `self-error-plan` | #52 | planificación sobre estado-externo asumido sin verificar (`list_issues` no leído) |
| `narrative-drag-numeric` | #54 | contador numérico inflado con offset narrativo sin verificar contra fuente primaria |

**Patrón meta**: 3 de 4 tipos involucran no-re-derivar contra fuente primaria. Esta es la razón estructural de la regla emergente #3.
