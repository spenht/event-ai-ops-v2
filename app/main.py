from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .routes.whatsapp import router as whatsapp_router
from .routes.payments import router as payments_router
from .routes.tickets import router as tickets_router
from .routes.automation import router as automation_router
from .routes.short_urls import router as short_urls_router
from .routes.broadcasts import router as broadcasts_router
from .routes.spartans import router as spartans_router
from .routes.checkin import router as checkin_router
from .routes.calls_api import router as calls_api_router
from .routes.webrtc_api import router as webrtc_api_router
from .routes.telnyx_webhooks import router as telnyx_webhooks_router
from .routes.call_media_ws import router as call_media_ws_router
from .routes.ticket_issue import router as ticket_issue_router
from .routes.lead_capture import router as lead_capture_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Event AI Ops v2")

# CORS — allow dashboard and other frontends to call our API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow any origin (landing pages, dashboard, etc.)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(whatsapp_router)
app.include_router(payments_router)
app.include_router(tickets_router)
app.include_router(automation_router)
app.include_router(short_urls_router)
app.include_router(broadcasts_router)
app.include_router(spartans_router)
app.include_router(checkin_router)
app.include_router(calls_api_router)
app.include_router(webrtc_api_router)
app.include_router(telnyx_webhooks_router)
app.include_router(call_media_ws_router)
app.include_router(ticket_issue_router)
app.include_router(lead_capture_router)


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Stripe checkout redirect pages
# ---------------------------------------------------------------------------

_PAGE_STYLE = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .card {
    max-width: 480px;
    width: 100%;
    text-align: center;
    padding: 48px 32px;
    border-radius: 20px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.08);
  }
  .icon { font-size: 72px; margin-bottom: 16px; }
  h1 { font-size: 28px; margin-bottom: 12px; }
  p { font-size: 17px; color: #555; line-height: 1.6; margin-bottom: 24px; }
  .btn {
    display: inline-block;
    padding: 14px 36px;
    border-radius: 12px;
    font-size: 17px;
    font-weight: 600;
    text-decoration: none;
    color: #fff;
    transition: transform 0.15s;
  }
  .btn:hover { transform: scale(1.04); }
  .success-bg { background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%); }
  .success-card { background: #fff; }
  .success-btn { background: #16a34a; }
  h1.success-title { color: #15803d; }
  .cancel-bg { background: linear-gradient(135deg, #fefce8 0%, #fef3c7 100%); }
  .cancel-card { background: #fff; }
  .cancel-btn { background: #d97706; }
  h1.cancel-title { color: #92400e; }
  .footer { margin-top: 28px; font-size: 13px; color: #999; }
</style>
"""


@app.get("/vip/success", response_class=HTMLResponse)
def vip_success():
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pago Exitoso - Beyond Wealth</title>{_PAGE_STYLE}</head>
<body class="success-bg">
  <div class="card success-card">
    <div class="icon">🎉</div>
    <h1 class="success-title">¡Pago exitoso!</h1>
    <p>Tu boleto VIP para <strong>Beyond Wealth Miami</strong> ha sido confirmado.<br>
    En unos momentos recibirás tu boleto con QR por WhatsApp.</p>
    <a href="https://wa.me/17543549055" class="btn success-btn">Regresar a WhatsApp</a>
    <div class="footer">Beyond Wealth Miami 2026 &bull; Spencer Hoffmann</div>
  </div>
</body></html>"""


@app.get("/vip/cancel", response_class=HTMLResponse)
def vip_cancel():
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pago No Completado - Beyond Wealth</title>{_PAGE_STYLE}</head>
<body class="cancel-bg">
  <div class="card cancel-card">
    <div class="icon">😕</div>
    <h1 class="cancel-title">Pago no completado</h1>
    <p>Parece que el pago no se pudo procesar o fue cancelado.<br>
    No te preocupes, puedes intentarlo de nuevo desde WhatsApp.</p>
    <a href="https://wa.me/17543549055" class="btn cancel-btn">Regresar a WhatsApp</a>
    <div class="footer">Beyond Wealth Miami 2026 &bull; Spencer Hoffmann</div>
  </div>
</body></html>"""
