# 🚨 ALERT — origin/main del repo event-ai-ops-v2 fue sobrescrito con la cadena watchdog (commit legítimo del proyecto orphaned en remote)

**Timestamp (UTC):** 2026-06-27T14:04Z
**Run:** MIA WATCHDOG #193 (Opus 4.7, Anthropic cloud)
**Severidad:** ALTA — main del repo de eventos contiene 52 reportes watchdog en vez del código del proyecto
**Reversibilidad:** posible (mi local clone + GitHub reflog ~90d), pero requiere `git push --force` autorizado por ti

## Resumen (Ley #15)

El watchdog lleva 192 runs cayendo en este repo. Mi report #192 afirmó que el push fue "fast-forward consolidación". **FALSO.** Este run #193 verificó:

```bash
$ git merge-base --is-ancestor 9d154dd c66899f && echo YES || echo NO
NO

$ git branch -r --contains 9d154dd
# (vacío)

$ git tag --contains 9d154dd
# (vacío)

$ git for-each-ref --format='%(refname)' refs/remotes refs/tags | \
    xargs -I{} sh -c 'git merge-base --is-ancestor 9d154dd {} 2>/dev/null && echo {}'
# (vacío — 9d154dd reachable desde 0 refs remotos)
```

**Estado actual de origin/main:**
- HEAD: `c66899f` = tip de la cadena de 52 commits de reportes watchdog
- merge-base con `9d154dd` (último commit legítimo del proyecto): **∅ (disjoint)**
- El commit `fix(calls): create_call_record envía lead_id=NULL explícito (#10)` ya no está accesible desde main

## Cómo pasó (mejor hipótesis, sin inventar)

Issue #11 (creada en run #31) describió la cadena como "huérfanos en detached HEAD que se van con el container". En ese momento, origin/main aún era `9d154dd`. Entre #31 y #187 (los reportes intermedios son los #32-#186 que tampoco te llegaron), algún run hizo `git push origin HEAD:main` desde detached HEAD. Como la cadena es disjoint del proyecto, ese push fue NON-fast-forward — debió ser rechazado o forzado. Aparentemente origin permite force-push a main (no hay branch protection), así que aceptó el reemplazo silenciosamente. El report #187 explícitamente dice "tras git push origin HEAD:main el server aceptó fast-forward e7a3c1b..fa16c01" — pero ese ya era el estado post-sobrescritura, no la sobrescritura misma.

## Tags / branches paralelos (lo que SÍ sobrevive)

```
v1.0.0       → ? (no verificado SHA)
v1.0.0-rc1   → ?
v1.1.0       → 0770f71  (historia distinta de 9d154dd)
v2.0-stable  → aac3dc0  (incluye "Fix address overflow on ticket", historia paralela al fix de calls)
```

**Branches abiertas con código del proyecto:**
```
origin/chore/finance-now-string-literal-fix
origin/chore/replace-utcnow-with-timezone-aware
origin/claude/charming-hertz
origin/fix/call-record-empty-lead-id    ← probablemente la versión PR del fix de 9d154dd
origin/fix/manual-dial-empty-lead-id
origin/fix/webrtc-credential-error
```

El fix de 9d154dd probablemente tiene gemelos en `fix/call-record-empty-lead-id` y/o en `v2.0-stable`, pero NO verifiqué patch-equivalencia. Tu equipo (Diego/Alvaro/Marcelo según event-ai-ops-brain) debe confirmar.

## Opciones de recovery (TÚ DECIDES)

### Opción R1 — Restaurar main a 9d154dd (recomendada si el código de eventos es lo importante)

```bash
# Desde un clone reciente (este container lo tiene):
git push --force origin 9d154dd:main
```

Efecto: origin/main vuelve a ser código del proyecto. La cadena de 52 reportes se mueve a refs/heads/watchdog-history-2026-06-09-to-2026-06-27 (si la preservas antes):

```bash
git push origin c66899f:refs/heads/watchdog-history-archive
git push --force origin 9d154dd:main
```

**Riesgo:** destructivo. Si algún miembro del equipo basó trabajo en c66899f (improbable, es chain de reportes), se pierde.

### Opción R2 — Restaurar main a v2.0-stable (si v2.0-stable es lo canónico)

```bash
git push --force origin aac3dc0:main
```

Mismo patrón. Más limpio si v2.0-stable es la versión "buena" actual.

### Opción R3 — Dejar todo como está + reapuntar cron

No restaurar. Aceptar que main es ahora chain de watchdog. Tu equipo trabaja desde `v2.0-stable` o branches `fix/*`. Reapuntar cron a `spenht/mia-watchdog` (Issue #11 opción A) para que la cadena deje de crecer.

**Costo:** confuso para nuevos contribuidores que clonen y vean reportes watchdog como "main".

### Opción R4 — Combinada (más segura)

1. Archivar la cadena: `git push origin c66899f:refs/heads/watchdog-history-archive` (preserva los 52 reportes).
2. Restaurar main: `git push --force origin 9d154dd:main` (o `aac3dc0` si prefieres v2.0-stable).
3. Reapuntar cron a `spenht/mia-watchdog`.
4. Resolver Issue #11 con link a este ALERT.

## Lo que NO hice (y por qué)

- ❌ **No** `git push --force` por mi cuenta. CLAUDE.md global: "NEVER run destructive git commands ... unless the user explicitly requests these actions." Force-push a main = destructivo.
- ❌ **No** rewrote tags ni branches. Mismo motivo.
- ❌ **No** abrí PR de "restore main" porque no puedo evaluar si v2.0-stable o 9d154dd es la base correcta — eso es decisión tuya / del equipo.
- ✅ Sí escribí este ALERT + watchdog report honesto.
- ✅ Sí dispararé PushNotification (la rutina existe para esto).

## Por qué este momento importa

Issue #11 lleva 74h silent. Mi #192 te dijo "fast-forward consolidación" — era falso. Sin esta corrección, los próximos runs seguirían perpetuando la afirmación incorrecta. La cadena no está rota (los reportes se siguen acumulando), pero el repo de tu equipo de eventos sí lo está. Cada día que pase con main = chain de watchdog es un día de confusión para Diego/Alvaro/Marcelo si vuelven a clonar.

— MIA WATCHDOG, run #193 (Opus 4.7), Ley #15 + Ley #21
