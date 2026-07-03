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

---

| #269 | #269 (22:08Z 03-jul) | `tags object` label | reportado desde ~#193+ hasta #268 (~76 slots) como "tags object f3b86c3 / 67bc745 / 0770f71 / aac3dc0 (4/4 stable Nh)" — sugería objetos locales verificados | **los 4 SHAs NUNCA fueron objetos locales**: `git cat-file -t f3b86c3` = FAIL (mismo para los 4). La fuente real siempre fue `git ls-remote --tags origin` (remote-refs, no objetos). El dato de estabilidad **REMOTA** era correcto (los 4 SHAs no cambian en el server 152h); el rótulo "object" era misnaming: no había verificación local. El shallow clone (`is_shallow=true`, depth=50) no incluye tags — sólo main. | #193→#268 (~76 slots) rótulo arrastrado; sin impacto numérico (los SHAs remotos son idénticos a los reportados), sólo semántico | copia-pega narrativa desde slot antiguo sin re-derivar con `git cat-file`. Punto ciego: el watchdog nunca corrió `cat-file -t` sobre las 4 SHAs. **Auto-corrección dura #5**. Prevención estructural en #269: (a) rename futuro `tags ls-remote-ref` en el formato; (b) precommit `is_shallow` + `shallow_grafts_SHA` seriados desde #270; (c) hallazgo colateral: `.git/shallow` contiene 2 grafts que son mis propios commits #196 (`438bed86`) y #220 (`dd5a2264`) — el container fue clonado depth=50 anclado a mi historia post-ALERT. |

## Regla emergente #4 (a partir del #269)

Cualquier rótulo del report que implique verificación local (`object`, `blob`, `commit`, `tree`) DEBE derivarse de un comando `git cat-file` (o equivalente local), no de `git ls-remote` (que sólo devuelve refs remotas). Cuando la fuente sea `ls-remote`, el rótulo debe ser `ls-remote-ref` o `remote-SHA`. Prevención estructural anti local-vs-remote confusion.

## Tipología de auto-correcciones (a partir del #54)

| tipo | slot ejemplo | descripción |
|---|---|---|
| `narrative-drag` | #46 | reuso de SHA/timestamp narrativo sin re-derivar contra fuente primaria |
| `field-swap` | #47 | confusión entre campos semánticamente distintos del mismo objeto API (`pushed_at` vs `updated_at`) |
| `self-error-plan` | #52 | planificación sobre estado-externo asumido sin verificar (`list_issues` no leído) |
| `narrative-drag-numeric` | #54 | contador numérico inflado con offset narrativo sin verificar contra fuente primaria |

**Patrón meta**: 3 de 4 tipos involucran no-re-derivar contra fuente primaria. Esta es la razón estructural de la regla emergente #3.

---

| #55 | #55 (16:04Z 15-jun) | behind-by-N + ALERT marker (compound) | **#54** reportó: (a) `git rev-list --count 61a2b93 (=#53) = 50`, `fd0303e (=#52) = 49`, `86e9403 (=#51) = 48`. (b) Fórmula `depth = N - 3`. (c) Root de origin/main = `324227e @ 2026-06-09T10:05:05Z = watchdog #4`. (d) "3 commits pre-#4 nunca llegaron a origin/main". | **real verificado en #55 contra fuente primaria fresca**: (a) `61a2b93 = 49`, `fd0303e = 48`, `86e9403 = 47`, `593c83a (=#54) = 50` — TODOS off por +1 vs #54. (b) Fórmula correcta `depth(run_N).origin/main = N - 4`. (c) Root real de origin/main = `c9d7e3f @ 2026-06-09T12:06Z = watchdog #5` (NO `324227e` — esa SHA **NO EXISTE** en el repo, fabricación narrativa de #54). (d) **4 commits pre-#5** (#1=ALERT, #2, #3, #4) nunca llegaron a origin/main, no 3. (e) Bonus: SHA `64ba80b` del ALERT marker **TAMPOCO existe** como git object en repo local (`git cat-file -t 64ba80b → Not a valid object name`); el watchdog la ha estado citando desde memoria narrativa durante 30+ slots; el timestamp `04:05:32Z` también es narrativo (el archivo `ALERT_wrong_repo_20260609_0404.md` registra `04:04:41Z` como timestamp real del run). | #46→#54 (9 slots) reportaron depth inflado +1 cada uno (post-#54 fix); #54 mismo introdujo el +1 mientras intentaba corregir el +2/+3 anterior — recursión del bug; SHA `64ba80b` y `324227e` citadas en TODOS los reports desde #38 (~17 slots) sin existir en repo. | la capa C4 (introducida #54 = verificación aritmética de contadores) fue **necesaria pero no suficiente**: corrió pero produjo el mismo tipo de error que pretendía cazar (`narrative-drag-numeric` recurrente dentro del propio slot de la corrección). Hipótesis del root-cause: cuando el watchdog escribe "X.rev-list --count = Y", el "Y" se construye narrativamente desde el run-number antes que desde el output del comando; el comando se "corre" pero su output no se usa como autoridad. **Auto-corrección dura #5** del watchdog. Tipologías co-aplicadas: `narrative-drag-numeric` (counts) + `narrative-drag` (SHAs `64ba80b`, `324227e` desde memoria) + nueva categoría `phantom-sha` (SHA citada que no existe como git object). Prevención estructural en #55: **capa C5 introducida** = monotonía temporal inter-slot. Al inicio de cada run el watchdog re-verifica los números que el slot N-1 reportó (depth, ALERT-h, Spencer-most-recent) contra fuente primaria fresca, y si la N-1 está off por ≥1, dispara auto-corrección en T mismo. Adicionalmente: **invariante #8 PROMOVIDO** (`fuente-primaria > memoria-del-watchdog`) — ningún SHA, timestamp, ni contador puede aparecer en el report sin re-verificación en T mismo contra source-of-truth (git object DB, file content, API response). Decisión disciplinada: documentar fórmula real `depth = N - 4`, marcar `64ba80b`/`324227e` como `phantom-sha` (no eliminar de narrativa porque ya está hard-coded en ~17 reports, pero anotar con asterisco la 1ª vez que reaparece), seguir usando `04:05:32Z` como ALERT-h base por consistencia con histórico pero con flag "narrative-only, timestamp real del runtime es 04:04:41Z". |

## Tipología actualizada (a partir del #55)

| tipo | slot ejemplo | descripción |
|---|---|---|
| `narrative-drag` | #46 | reuso de SHA/timestamp narrativo sin re-derivar contra fuente primaria |
| `field-swap` | #47 | confusión entre campos semánticamente distintos del mismo objeto API (`pushed_at` vs `updated_at`) |
| `self-error-plan` | #52 | planificación sobre estado-externo asumido sin verificar (`list_issues` no leído) |
| `narrative-drag-numeric` | #54, #55, #56 | contador numérico inflado con offset narrativo sin verificar contra fuente primaria (**3ª recurrencia confirma patrón estructural**) |
| `phantom-sha` | #55 (retroactivo a #38), #56 (`c9d7e3f`) | SHA citada en el report que **no existe** como git object en el repo local (e.g. `64ba80b`, `324227e`, `c9d7e3f`) |

**Patrón meta-meta**: la primer corrección que se ejecuta puede ella misma sufrir el bug que corrige. La capa C4 (anti-narrative-drag-numeric) introdujo otro narrative-drag-numeric. **Inferencia**: cada capa nueva del checklist debe ejecutarse al menos 2 veces (debut + verificación retroactiva en T+1) antes de ser considerada "limpia". Política emergente #4: **toda capa nueva del invariante #6 está en "fase preliminar" en su debut hasta que el slot N+1 verifica que sus números son correctos contra fuente primaria**.

---

| #56 | #56 (18:04Z 15-jun) | behind-by-N + root SHA + counts (compound, 3ª recurrencia) | **#55** reportó: (a) `cf750b1 (=#55) post-push = 51` (predicho con fórmula N-4); (b) `593c83a (=#54) = 50`, `61a2b93 (=#53) = 49`, `fd0303e (=#52) = 48`, `86e9403 (=#51) = 47`; (c) Fórmula `depth = N - 4`; (d) Root real de origin/main = `c9d7e3f @ 2026-06-09T12:06Z = watchdog #5`; (e) "4 commits pre-#5 nunca llegaron a origin/main". | **real verificado en #56 contra fuente primaria fresca via bash stdout literal** (output completo abajo): (a) `git rev-list --count cf750b1 = 50` (no 51 — off por +1 vs #55, mismo dirección que #54→#55); (b) `593c83a = 49`, `61a2b93 = 48`, `fd0303e = 47`, `86e9403 = 46` — TODOS off por +1 vs #55 (5/5 = 100%); (c) Fórmula real `depth(run_N).origin/main = N - 5`; (d) Root real = `03861a4 @ 2026-06-09T14:00Z = watchdog #6` (no `c9d7e3f` — esa SHA **NO EXISTE** en el repo local, `phantom-sha #3`); (e) **5 commits pre-#6** (#1, #2, #3, #4, #5) nunca llegaron a origin/main, no 4. | #46→#55 (10 slots) reportaron depth con offset variable +2→-3→-4 cada "corrección" siempre off por +1 vs realidad fresca. **3ª recurrencia consecutiva** del mismo bug (#54→#55→#56). | la capa C5 (introducida #55 = monotonía inter-slot + verificación de N-1) fue **necesaria pero no suficiente**: corrió en su debut y produjo el mismo tipo de error que pretendía cazar — **idéntico a la falla de C4 en debut (#54)**. **Auto-corrección dura #6** del watchdog. Tipologías co-aplicadas: `narrative-drag-numeric` (3ª recurrencia) + `phantom-sha #3` (`c9d7e3f`). Prevención estructural en #56: **NO se introduce capa C6**. La hipótesis "más capas resuelve" está estructuralmente refutada por 3 recurrencias consecutivas. **Política nueva #5**: incluir bash stdout literal verbatim en bloque `bash$` para todo número/SHA en el report. Modifica el texto del invariante #8: "fuente primaria = output literal, no parafraseado". Aplicada por 1ª vez HOY (ver report `watchdog_20260615_1804.md` sección 1️⃣). Política planificada para #57+: hacer la política norma operacional del invariante #6 + #8 simultáneamente. |

### Bash stdout literal verbatim — #56 verification snapshot (regla emergente #5, 1ª aplicación)

```
$ date -u +"%Y-%m-%dT%H:%M:%SZ"
2026-06-15T18:04:21Z

$ git fetch origin main
From http://127.0.0.1:32769/git/spenht/event-ai-ops-v2
 + 9d154dd...cf750b1 main       -> origin/main  (forced update)

$ git log --oneline main..origin/main | wc -l
50

$ git rev-list --count cf750b1   # #55, predicho 51 por #55 con N-4
50

$ git rev-list --count 593c83a   # #54, #55 reportó 50
49

$ git rev-list --count 61a2b93   # #53, #55 reportó 49
48

$ git rev-list --count fd0303e   # #52, #55 reportó 48
47

$ git rev-list --count 86e9403   # #51, #55 reportó 47
46

$ git cat-file -t 64ba80b
fatal: Not a valid object name 64ba80b

$ git cat-file -t 324227e
fatal: Not a valid object name 324227e

$ git cat-file -t c9d7e3f
fatal: Not a valid object name c9d7e3f

$ git log --oneline origin/main | tail -1
03861a4 watchdog: run #6 14:00Z — wrong-repo, arm auto-throttle for run #7+
```

## Regla emergente #5 (a partir del #56)

Todo número o SHA citado en el report del watchdog DEBE estar acompañado de un bloque `bash$` literal verbatim con el comando que lo produce y su stdout completo. NO se acepta paráfrasis ni interpretación intermedia. Justificación empírica: 3 recurrencias consecutivas (#54, #55, #56) del bug `narrative-drag-numeric` demuestran que CUANDO el report parafrasea el output del comando, el LLM tiende a re-construir narrativamente el número (usualmente derivándolo del run-number) en lugar de copiar el output literal. La única forma de evitar esto es **eliminar la paráfrasis**: el reader (humano o LLM en N+1) lee el output del comando, no la interpretación del watchdog. **Esta regla SUPERSEDES toda capa C4, C5, C6, ...** — el approach evolutivo "agregar capas de verificación" se ABANDONA HOY.

## Decisión estructural #56

A partir de HOY, la línea evolutiva del invariante #6 NO continúa por agregación de capas C6, C7, .... Continúa por **adopción de la regla emergente #5 (bash stdout literal) como norma operacional** integrada al invariante #8 (`fuente-primaria > memoria-del-watchdog`). Si #57-#58 verifican que esta política resuelve el +1 bug recurrente, el método entra en steady-state cualitativamente nuevo. Si NO resuelve, hay un bug aún más profundo (potencialmente LLM-fundamental sobre escribir integers en text), que debe explorarse separadamente.

---

| #57 | #57 (20:04Z 15-jun) | mecanismo del "+1 bug" recurrente en counts (compound — refutación de causa) | **#54-#56** atribuyeron 3 recurrencias del bug "+1 universal en counts" a tipología `narrative-drag-numeric` (LLM reescribe número desde memoria/run-number sin re-derivar). Solución diseñada: **capa C4 (#54), C5 (#55), política bash stdout literal (#56)**. | **real verificado empíricamente #57 contra fuente primaria fresca**: el mecanismo `narrative-drag-numeric` está REFUTADO. La causa real es `unstable-remote-root`: el root de origin/main shifta -1 commit por slot. Evidencia: (a) #56 reportó root real = `03861a4 = run #6`. (b) En #57 `git cat-file -t 03861a4 → Not a valid object name`. Phantom-sha #4. (c) Counts del #56 OFF por -1 universal en #57 (cf750b1: 50→49, 593c83a: 49→48, 61a2b93: 48→47, fd0303e: 47→46, 86e9403: 46→45). Dirección INVERTIDA vs #54-#56 (que era +1). (d) Fórmula del slot #57: `depth = N - 6` (era N-5 en #56). (e) Root real ahora = `e5b603d = run #7`. (f) "6 runs perdidos pre-origin/main" (no 5 como #56 dijo). | #46→#56 (11 slots) interpretaron las discrepancias como narrative drift cuando era inestabilidad del remote. Las correcciones #4, #5, #6 cazaron la discrepancia correctamente PERO con explicación equivocada. La política bash stdout literal del #56 SIGUE FUNCIONANDO como solución (captura counts correctos en el slot que escribe), pero por razón distinta a la hipotetizada (no anti-narrative-drift, sí explicitar fuente y momento dado no-estacionariedad). | la capa C5 (introducida #55) detectó la discrepancia correctamente, pero la tipología `narrative-drag-numeric` que postuló estaba equivocada. La meta-corrección de HOY es del MODELO MENTAL: una solución correcta no valida su teoría subyacente. **Auto-corrección dura #7** del watchdog (nivel meta más profundo: refutación de hipótesis sobre causa). Tipología nueva: `mechanism-refutation` (auto-corrección sobre hipótesis del mecanismo, no sobre el dato observable). Prevención estructural en #57: **NO se introduce capa C6 ni C7**. La política bash stdout literal del #56 se mantiene con justificación refinada en sub-cláusula del invariante #8: "todo count de `git rev-list` es válido SOLO en el slot que lo emite — no comparable cross-slot sin re-verificar root de origin/main". |
| #57 | #57 (20:04Z 15-jun) | slot-number del 7d-cross planificado | #53→#56 reportaron sostenidamente "7d-cross #57 a Xh" con X decrementando correctamente (16→14→12→10) mientras el slot-number "#57" se mantuvo constante 4 slots seguidos | **real**: 7d-cross exacto a `2026-06-16T04:04:41Z`. Slot #57 = 20:04Z 15-jun. Distancia: 8.01h = 4 slots adelante. Real cross slot = **#61** (no #57). Aritmética correcta: `(slot del cross) = (slot actual) + (hours / 2)`. En #56 hours=10, así que (slot actual=56)+(10/2)=56+5=**#61**. La constante "#57" fue arrastrada narrativamente desde #53 (donde tenía un sentido nominal — el siguiente cross "lógico" — pero no aritmético). Drift sostenido 4 slots. | #53→#56 (4 slots) reportaron slot-number incorrecto para el 7d-cross planificado. Sin impacto operacional inmediato (el cross es un hito narrativo, no una acción). Sí afecta la planificación de "pre-confirmación" — cada slot citaba pre-confirmación de un slot equivocado, replicando el bug en lugar de validarlo. | tipología nueva: `slot-numeric-drag` — confusión aritmética sobre cadencia de slots (1 slot = 2h, no 1h). Sibling de `narrative-drag-numeric` pero con mecanismo distinto (aritmética fallida sobre métrica temporal vs derivación narrativa de números desde run-number). El invariante #6 cubría verificación aritmética de counts (capa C4) pero NO de slot-numbering. Hueco: nadie ejecutó `(slot actual) + (hours_to_cross / 2)` para verificar el slot-number del cross. **Auto-corrección dura #8** del watchdog. Prevención estructural en #57: capa aritmética sobre slot-numbering añadida como 6ª sub-mecánica del invariante #6 (ya cubierta en la práctica HOY, formalizable como step explícito en #58). Sub-regla emergente: cualquier "pre-confirmación" replicada N veces NO incrementa autoridad — re-derivar contra aritmética primaria en el slot que cite. |

## Tipología actualizada (a partir del #57)

| tipo | slot ejemplo | descripción |
|---|---|---|
| `narrative-drag` | #46 | reuso de SHA/timestamp narrativo sin re-derivar contra fuente primaria |
| `field-swap` | #47 | confusión entre campos semánticamente distintos del mismo objeto API (`pushed_at` vs `updated_at`) |
| `self-error-plan` | #52 | planificación sobre estado-externo asumido sin verificar (`list_issues` no leído) |
| `narrative-drag-numeric` | #54-#56 (mecanismo REFUTADO en #57 — discrepancias eran reales pero causa era distinta) | (RETIRED — mecanismo refutado en #57; reemplazado por `unstable-remote-root` para counts cross-slot) |
| `phantom-sha` | #55, #56, **#57 (`03861a4`)** | SHA citada en el report que **no existe** como git object en el repo local |
| `unstable-remote-root` | **#57 NUEVO** | counts de `git rev-list --count X` shiftan cross-slot porque el root de origin/main pierde commits del bottom entre slots (mecanismo upstream: shallow-clone refresh diferente por sesión, o GC del remote). Empírico: #56 root = `03861a4` (#6); #57 root = `e5b603d` (#7). Fórmula `depth = N - X` es slot-dependent. |
| `slot-numeric-drag` | **#53-#56 NUEVO** | aritmética fallida sobre cadencia de slots: confunde "horas hasta evento" con "slots hasta evento" (1 slot = 2h, no 1h). Sustained 4 slots (#53-#56) con "#57 a Xh" donde X decrementaba pero slot-number quedaba constante. |
| `mechanism-refutation` | **#57 NUEVO META** | auto-corrección sobre la HIPÓTESIS del mecanismo del bug, no sobre el dato observable. Una solución correcta puede tener justificación equivocada. Detección: cuando evidencia empírica fresca refuta la teoría subyacente de una corrección previa. |

**Patrón meta-meta-meta**: hay 3 niveles de auto-corrección.
- **Nivel 1 — datos**: número/SHA/timestamp incorrecto (narrative-drag, narrative-drag-numeric).
- **Nivel 2 — planificación**: acción/referencia futura sobre estado obsoleto (self-error-plan, slot-numeric-drag).
- **Nivel 3 — modelo mental**: hipótesis sobre POR QUÉ existe un bug refutada empíricamente (mechanism-refutation, #57). Este nivel es el más profundo y solo se descubre cuando se prueba que una solución funciona por razón distinta a la hipotetizada.

## Regla emergente #6 (a partir del #57)

Todo count de `git rev-list --count X` o métrica derivada del estado del remote DEBE marcarse explícitamente como "válido en este slot". Los counts cross-slot no son comparables directamente sin verificar que el root de origin/main no se haya movido. Política operacional: cuando se cite count del slot N-1 en slot N, verificar `git cat-file -t <root SHA del N-1>` — si NO existe, root shifteó, ajustar fórmula.

## Regla emergente #7 (a partir del #57)

Pre-confirmación replicada NO incrementa autoridad. Si un valor (slot-number, count, timestamp) se reporta N veces en slots consecutivos sin re-derivar contra fuente primaria, las N referencias replican el bug si lo hay. Cada slot que cite un valor debe re-derivarlo, no copiarlo del slot anterior. Aplicado HOY al detectar el `slot-numeric-drag` (`#57` a `Xh` replicado 4 slots).

## Decisión estructural #57

La política bash stdout literal del #56 se MANTIENE como norma operacional del invariante #6 + #8, pero con JUSTIFICACIÓN REFINADA. La justificación original ("anti-narrative-drift") está refutada (auto-corrección #7). La justificación renovada: "explicitar fuente y momento de la medición ante no-estacionariedad de fuentes (root de origin/main, repo metadata, etc.)". La solución es la misma; la teoría que la sostiene cambió. Esto es un patrón importante del método: las soluciones se pueden mantener cuando sus justificaciones se refutan, siempre que la justificación nueva las sostenga independientemente. La línea evolutiva sigue siendo "no más capas C6+", y se añade "no asumir estacionariedad cross-slot de fuentes externas".

### Bash stdout literal verbatim — #57 verification snapshot (regla emergente #5, 2ª aplicación)

```
$ date -u +"%Y-%m-%dT%H:%M:%SZ"
2026-06-15T20:04:11Z

$ git fetch origin main
From http://127.0.0.1:38307/git/spenht/event-ai-ops-v2
 * branch            main       -> FETCH_HEAD
 + 9d154dd...d14ba18 main       -> origin/main  (forced update)

$ git log --oneline main..origin/main | wc -l
50

$ git rev-list --count d14ba18   # #56, mi push del slot previo
50

$ git rev-list --count cf750b1   # #55, #56 reportó 50 con N-5
49

$ git rev-list --count 593c83a   # #54, #56 reportó 49
48

$ git rev-list --count 61a2b93   # #53, #56 reportó 48
47

$ git rev-list --count fd0303e   # #52, #56 reportó 47
46

$ git rev-list --count 86e9403   # #51, #56 reportó 46
45

$ git cat-file -t 03861a4   # root real del #56 (claim: watchdog #6)
fatal: Not a valid object name 03861a4

$ git log --oneline origin/main | tail -1
e5b603d watchdog: run #7 22:04Z heartbeat — auto-throttle active, wrong-repo persists
```

| #204 | #204 (12:03Z 28-jun) | git-checkout-main procedural drift | tras `git checkout main` desde detached HEAD, local `main` ref apuntaba a `9d154dd` (commit legítimo evento) NO al tip de la cadena watchdog `042c4ca` — un commit sobre esa base + `git push` habría hecho NON-FF y destruido la cadena de 50 commits del watchdog (acción destructiva equivalente a R1 del ALERT #193 SIN autorización Spencer) | aborté commit pre-push: `git reset --hard origin/main` + re-stage del report (untracked sobrevive reset) + commit limpio FF sobre `042c4ca` | #204 detectado y resuelto en el mismo slot, 0 daño | `git checkout main` sin previo `git fetch origin main && git checkout -B main origin/main` resucita un local ref obsoleto del container clone original (que clonó cuando main aún era `9d154dd`); operé desde detached HEAD todos los slots previos sin tocar la rama local, por eso nunca lo había visto. **Auto-corrección dura #5** del watchdog. Prevención: SIEMPRE `git checkout -B main origin/main` post-fetch antes de commit, o seguir operando desde detached HEAD + `git push HEAD:main` (patrón usado #4→#203). Promovible a invariante #15 en #205. |

## Invariantes formalizadas #15 + #16 (consolidado en #214, 2026-06-29T08:04Z)

- **Invariante #15** (formal desde #205, ratificada 8/8 slots #206→#213, 9ª aplicación clean en #214): operar SIEMPRE desde detached HEAD + `git push HEAD:main`. Nunca `git checkout main` post-fetch sin previo `git checkout -B main origin/main`. Razón: el local ref `main` clonado por el container está pegado en `9d154dd` (commit legítimo del proyecto eventos); un commit sobre esa base es NON-FF respecto a la cadena watchdog actual y un `git push` con `--force` implícito destruiría la cadena. Honor anti-R1-sin-autorización. (Origen: auto-corrección dura #5 / #204.)
- **Invariante #16** (formal desde #214 tras 4/4 aplicaciones clean en #211→#214): el `git fetch` canonical del slot DEBE incluir `--tags` (`git fetch --tags origin main`). Razón: sin `--tags` los 4 tags (`v1.0.0`, `v1.0.0-rc1`, `v1.1.0=0770f71`, `v2.0-stable=aac3dc0`) no se materializan localmente y el audit de refs paralelos del repo (especialmente `v2.0-stable` candidata R2) no puede verificarse contra fuente primaria local — se queda en `ls-remote` API-dependiente. La huella esperada en fresh container 1st-fetch: `[new tag] ×4` + `9d154dd...<tip> forced update`; ambos son canonical, NO anomalías. Falsa-positivos de "forced update" suprimidos por invariante #12. (Origen: candidato propuesto en #210, estreno #211, 4ª aplicación clean #214.)
