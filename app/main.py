from __future__ import annotations

import logging

from fastapi import FastAPI

from .routes.whatsapp import router as whatsapp_router
from .routes.payments import router as payments_router
from .routes.tickets import router as tickets_router
from .routes.automation import router as automation_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Event AI Ops v2")

app.include_router(whatsapp_router)
app.include_router(payments_router)
app.include_router(tickets_router)
app.include_router(automation_router)


@app.get("/health")
def health():
    return {"ok": True}
