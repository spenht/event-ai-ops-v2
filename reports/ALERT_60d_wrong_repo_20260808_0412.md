# 🚨 ALERT — MIA Watchdog wrong-repo firing crosses 60 days

**Timestamp (UTC):** 2026-08-08T04:12:18Z
**Slot:** watchdog run #689 (Anthropic cloud, Opus 4.7, on-cron ~2h)
**Severity:** MEDIUM — no new failure mode; milestone bump on a
long-standing, previously-notified condition.
**Follow-up to:** `ALERT_wrong_repo_20260609_0404.md` (original), issue #11
(2026-06-13, silent 36d 16h).

## Qué cruzó

El trigger `mia-watchdog` sigue aterrizando en `spenht/event-ai-ops-v2`
(backend FastAPI de eventos — NO cluster MIA). Delta desde el primer
mis-fire registrado (`2026-06-09T04:04:41Z` según el ALERT original):

```
$ python3 - <<'PY'
from datetime import datetime, timezone
t0 = datetime(2026, 6, 9, 4, 4, 41, tzinfo=timezone.utc)
now = datetime(2026, 8, 8, 4, 12, 18, tzinfo=timezone.utc)
d = now - t0
print(f"elapsed: {d.days}d {d.seconds//3600}h {(d.seconds%3600)//60}m {d.seconds%60}s")
PY
elapsed: 60d 0h 7m 37s
```

60 días completos ↔ 30 días × 2 ciclos de scheduler (12 slots/día × 60d =
**~720 slots ejecutados** hasta este #689, con margen por gaps
esporádicos que no cambian el orden de magnitud).

## Por qué este slot y no otro

El slot #688 (02:11Z) explícitamente reservó la decisión de emitir este
ALERT al primer on-cron post-cruce nominal (~04:05Z). Este slot #689
(04:12:18Z) es ese primer on-cron: cruce +7m 37s. No es re-diagnóstico
ni nuevo hallazgo — es un bump silencioso pero visible en historial git
(`[ALERT]` prefix), sin PushNotification (invariante desde ~#40:
issue #11 ya es el canal accountable, y Spencer ha ignorado 36d de
notificaciones GH sobre ese hilo — un push extra no supera el filtro
que ya lo silenció).

## Estado de las opciones (opciones A/B/D del ALERT original)

Ninguna ejecutada. Las tres siguen viables y siguen siendo *acción
humana externa a este container*:

- **A** — reroute del trigger en code.claude.com al repo MIA correcto.
- **B** — crear `spenht/mia-watchdog-state` y reapuntar el cron ahí.
- **D** — pausar el trigger `mia-watchdog` en code.claude.com hasta
  resolver. **Sigue siendo la mitigación más barata** mientras no
  exista repo destino: elimina ~12 slots/día de cuota Opus 4.7
  quemada sin output útil.

Opción C (bootstrap MIA aquí en este repo) sigue desactivada — mezclaría
watchdog de trading con backend de eventos, contaminación innecesaria.

## Costos acumulados (orden de magnitud)

- **Slots ejecutados post-ALERT-original:** #31 → #689 = **~659 slots**
  útiles en cadena continua desde que se abrió issue #11 (los primeros
  30 fueron huérfanos pre-push-to-main).
- **Cuota Opus 4.7:** cada slot razona el contexto MIA completo antes
  de descartarlo por N/A. Aproximación conservadora: si cada slot cuesta
  ~$0.30-$0.50 en tokens Opus, el rango total es ~$200-$330 quemados
  en 60d sin output útil. (Opción D los ahorra desde el instante que
  se aplique.)
- **Ancho de banda de tu inbox:** 0. El mecanismo silencioso de invariante
  #40 evitó ping-spam a costa de que el problema no re-escale.

## Qué NO cambió con el cruce

- Ningún dato nuevo sobre wallet / bots / buckets / snipers. Sigue N/A.
- Ningún cambio humano en issue #11 (silente 36d 16h) ni en PR #12
  (silente 31d 2h — PR ajeno al watchdog, autor `tech-uvul`).
- Ningún fix aplicable desde este container (Ley #21: nada en este
  repo mueve la aguja a real money para el cluster MIA).

## Próximo paso natural

Slot #690 (~06:12Z) no requiere re-emitir este ALERT. Si al #690 tampoco
hay reroute/pausa, el próximo hito natural es *90 días* (~2026-09-07T04:05Z,
slot ~#1050), demasiado lejos para reservar decisión ahora — se
re-evaluará ~24-48h antes.

— MIA WATCHDOG (Opus 4.7, run #689, on-slot 2026-08-08T04:12Z)
