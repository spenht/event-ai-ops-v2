from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..deps import sb
from ..settings import settings
from ..services.google_sheets import sync_lead_to_all_leads_sheet

logger = logging.getLogger("checkin")

router = APIRouter(prefix="/checkin", tags=["checkin"])


# ── helpers ────────────────────────────────────────────────────────


def _get_checkin_count() -> int:
    """Return total unique check-ins from Supabase."""
    try:
        res = sb.table("touchpoints") \
            .select("lead_id") \
            .eq("event_type", "checkin") \
            .execute()
        return len(res.data) if res.data else 0
    except Exception:
        return 0


def _scanner_html(key: str) -> str:
    """Return the full-screen QR scanner HTML page."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Check-in — Beyond Wealth</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background:#0d0800;
  color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  height:100vh; height:100dvh;
  display:flex; flex-direction:column;
  overflow:hidden;
}}
.topbar {{
  text-align:center;
  padding:12px 16px;
  background:#1a1000;
  border-bottom:1px solid #d4af3733;
}}
.topbar h1 {{ font-size:18px; letter-spacing:2px; color:#fff; }}
.topbar .sub {{ font-size:13px; color:#d4af37; margin-top:2px; }}
#counter {{
  display:inline-block;
  background:#d4af3720;
  border:1px solid #d4af3740;
  border-radius:20px;
  padding:2px 12px;
  font-size:13px;
  color:#d4af37;
  margin-top:6px;
}}
#scanner-region {{
  flex:1;
  display:flex;
  align-items:center;
  justify-content:center;
  overflow:hidden;
  position:relative;
}}
#reader {{
  width:100%;
  max-width:500px;
}}
#reader video {{ border-radius:8px; }}
/* hide default html5-qrcode UI chrome */
#reader__dashboard_section_swaplink,
#reader__status_span,
#reader__header_message,
#reader img[alt="Info icon"] {{
  display:none !important;
}}
#result {{
  position:fixed;
  bottom:0; left:0; right:0;
  padding:20px 16px 28px;
  text-align:center;
  transform:translateY(100%);
  transition:transform 0.25s ease;
  z-index:100;
}}
#result.show {{ transform:translateY(0); }}
#result.success {{ background:#16a34a; }}
#result.already {{ background:#a16207; }}
#result.error {{ background:#dc2626; }}
#result .rname {{
  font-size:24px;
  font-weight:700;
  margin-bottom:4px;
}}
#result .rtier {{
  font-size:16px;
  font-weight:400;
  opacity:0.9;
}}
#result .rmsg {{
  font-size:14px;
  opacity:0.8;
  margin-top:4px;
}}
.loading {{
  position:fixed;
  top:50%; left:50%;
  transform:translate(-50%,-50%);
  font-size:14px;
  color:#665a3a;
}}
</style>
</head>
<body>
<div class="topbar">
  <h1>BEYOND WEALTH</h1>
  <div class="sub">Check-in</div>
  <div id="counter">Cargando...</div>
</div>
<div id="scanner-region">
  <div id="reader"></div>
  <div class="loading" id="loadingMsg">Iniciando camara...</div>
</div>
<div id="result">
  <div class="rname" id="rname"></div>
  <div class="rtier" id="rtier"></div>
  <div class="rmsg" id="rmsg"></div>
</div>

<script>
const KEY = "{key}";
let scanCount = 0;
let processing = false;
let scanner = null;

// ── load real count from server ──
async function loadCount() {{
  try {{
    const r = await fetch('/checkin/count?key=' + KEY);
    const j = await r.json();
    scanCount = j.count || 0;
    updateCounter();
  }} catch(e) {{}}
}}

function updateCounter() {{
  document.getElementById('counter').textContent = scanCount + ' asistente' + (scanCount === 1 ? '' : 's');
}}

// ── audio feedback ──
function beep(freq, ms) {{
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = freq;
    g.gain.value = 0.3;
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + ms / 1000);
  }} catch(e) {{}}
}}

// ── show result banner ──
function showResult(cls, name, tier, msg) {{
  const el = document.getElementById('result');
  document.getElementById('rname').textContent = name || '';
  document.getElementById('rtier').textContent = tier || '';
  document.getElementById('rmsg').textContent = msg || '';
  el.className = cls + ' show';
  setTimeout(() => {{ el.className = ''; }}, 2500);
}}

// ── parse QR payload ──
function parseQR(text) {{
  try {{
    // QR contains Python dict repr with single quotes
    const json = text.replace(/'/g, '"');
    return JSON.parse(json);
  }} catch(e) {{
    return null;
  }}
}}

// ── verify with backend ──
async function verify(data) {{
  if (processing) return;
  processing = true;
  try {{
    const res = await fetch('/checkin/verify?key=' + KEY, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(data),
    }});
    const j = await res.json();
    if (j.ok) {{
      // Always sync counter from server
      if (typeof j.total === 'number') {{
        scanCount = j.total;
        updateCounter();
      }}
      if (j.already) {{
        beep(400, 300);
        showResult('already', j.name, j.tier, 'Ya registrado');
      }} else {{
        beep(800, 150);
        showResult('success', j.name, j.tier, 'Check-in exitoso');
      }}
    }} else {{
      beep(200, 400);
      showResult('error', '', '', j.message || 'QR invalido');
    }}
  }} catch(e) {{
    beep(200, 400);
    showResult('error', '', '', 'Error de conexion');
  }}
  setTimeout(() => {{ processing = false; }}, 2000);
}}

// ── init scanner ──
function startScanner() {{
  scanner = new Html5Qrcode("reader");
  scanner.start(
    {{ facingMode: "environment" }},
    {{ fps: 10, qrbox: {{ width: 250, height: 250 }}, aspectRatio: 1.0 }},
    (text) => {{
      if (processing) return;
      const data = parseQR(text);
      if (data && data.ticket_id && data.code) {{
        verify(data);
      }} else {{
        beep(200, 400);
        showResult('error', '', '', 'QR no reconocido');
        processing = true;
        setTimeout(() => {{ processing = false; }}, 2000);
      }}
    }},
    () => {{}}
  ).then(() => {{
    document.getElementById('loadingMsg').style.display = 'none';
  }}).catch(err => {{
    document.getElementById('loadingMsg').textContent = 'Error: ' + err;
  }});
}}

loadCount();
startScanner();
</script>
</body>
</html>"""


# ── endpoints ──────────────────────────────────────────────────────


@router.get("/scan", response_class=HTMLResponse)
async def checkin_scanner(key: str = ""):
    """Serve the QR scanner page."""
    if not settings.checkin_key or key != settings.checkin_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return _scanner_html(key)


@router.get("/count")
async def checkin_count(key: str = ""):
    """Return total unique check-ins."""
    if not settings.checkin_key or key != settings.checkin_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return {"count": _get_checkin_count()}


@router.post("/verify")
async def checkin_verify(request: Request, key: str = ""):
    """Verify a scanned QR and register check-in."""

    # 1. Auth
    if not settings.checkin_key or key != settings.checkin_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    # 2. Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "Payload invalido"})

    ticket_id = (body.get("ticket_id") or "").strip()
    code = (body.get("code") or "").strip().upper()
    lead_id = (body.get("lead_id") or "").strip()

    if not ticket_id or not code:
        return JSONResponse({"ok": False, "message": "Datos incompletos"})

    # 3. Find ticket_created touchpoint
    try:
        res = sb.table("touchpoints") \
            .select("*") \
            .eq("event_type", "ticket_created") \
            .eq("lead_id", lead_id) \
            .execute()
    except Exception as exc:
        logger.error("checkin_lookup_failed err=%s", str(exc)[:300])
        return JSONResponse({"ok": False, "message": "Error al buscar boleto"})

    # Match by ticket_id in payload
    ticket_tp = None
    for tp in (res.data or []):
        payload = tp.get("payload") or {}
        if payload.get("ticket_id") == ticket_id:
            ticket_tp = tp
            break

    if not ticket_tp:
        # Fallback: search without lead_id filter (in case lead_id in QR differs)
        try:
            res2 = sb.table("touchpoints") \
                .select("*") \
                .eq("event_type", "ticket_created") \
                .execute()
            for tp in (res2.data or []):
                payload = tp.get("payload") or {}
                if payload.get("ticket_id") == ticket_id:
                    ticket_tp = tp
                    lead_id = tp.get("lead_id", lead_id)
                    break
        except Exception:
            pass

    if not ticket_tp:
        return JSONResponse({"ok": False, "message": "Boleto no encontrado"})

    # 4. Verify code
    stored_code = (ticket_tp.get("payload", {}).get("code") or "").strip().upper()
    if stored_code and code != stored_code:
        return JSONResponse({"ok": False, "message": "Codigo invalido"})

    # 5. Get lead info
    lead_name = ""
    tier = (ticket_tp.get("payload", {}).get("tier") or "").strip().upper()
    try:
        lr = sb.table("leads").select("name, status, tier_interest").eq("lead_id", lead_id).limit(1).execute()
        lead = (lr.data or [{}])[0]
        lead_name = (lead.get("name") or "").strip()
    except Exception:
        pass

    if not lead_name:
        lead_name = (body.get("email") or "").strip() or "Asistente"

    # 6. Check if already checked in
    try:
        existing = sb.table("touchpoints") \
            .select("lead_id") \
            .eq("event_type", "checkin") \
            .eq("lead_id", lead_id) \
            .limit(1) \
            .execute()
        if existing.data:
            total = _get_checkin_count()
            return JSONResponse({
                "ok": True,
                "already": True,
                "name": lead_name,
                "tier": tier or "GENERAL",
                "message": "Ya registrado",
                "total": total,
            })
    except Exception:
        pass

    # 7. Create check-in touchpoint
    try:
        sb.table("touchpoints").insert({
            "lead_id": lead_id,
            "channel": "door",
            "event_type": "checkin",
            "payload": {
                "ticket_id": ticket_id,
                "tier": tier,
                "code": code,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }).execute()
        logger.info("checkin_success lead=%s name=%s tier=%s", lead_id, lead_name, tier)
    except Exception as exc:
        logger.error("checkin_insert_failed err=%s", str(exc)[:300])
        return JSONResponse({"ok": False, "message": "Error al registrar"})

    # 8. Update lead status to ATTENDED in Supabase
    attended_status = f"{tier}_ATTENDED" if tier else "ATTENDED"
    try:
        sb.table("leads").update({
            "status": attended_status,
        }).eq("lead_id", lead_id).execute()
        logger.info("lead_status_updated lead=%s status=%s", lead_id, attended_status)
    except Exception as exc:
        logger.error("lead_status_update_failed lead=%s err=%s", lead_id, str(exc)[:300])

    # 9. Sync to Google Sheets (fire-and-forget, re-fetch with updated status)
    try:
        lr2 = sb.table("leads").select("*").eq("lead_id", lead_id).limit(1).execute()
        if lr2.data:
            asyncio.create_task(sync_lead_to_all_leads_sheet(lr2.data[0]))
    except Exception:
        pass

    # 10. Return with global count
    total = _get_checkin_count()
    return JSONResponse({
        "ok": True,
        "already": False,
        "name": lead_name,
        "tier": tier or "GENERAL",
        "message": "Check-in exitoso",
        "total": total,
    })
