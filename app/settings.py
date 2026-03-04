from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v != "" else default


@dataclass(frozen=True)
class Settings:
    # Core
    public_base_url: str = _env("PUBLIC_BASE_URL", "")  # e.g. https://calls-mx.fly.dev

    # Supabase
    supabase_url: str = _env("SUPABASE_URL", "")
    supabase_key: str = _env("SUPABASE_KEY", "")

    # Twilio
    twilio_account_sid: str = _env("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = _env("TWILIO_AUTH_TOKEN", "")
    twilio_whatsapp_from: str = _env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

    # OpenAI
    openai_api_key: str = _env("OPENAI_API_KEY", "")
    openai_model: str = _env("OPENAI_MODEL", "gpt-4o-mini")
    whatsapp_system_prompt_path: str = _env(
        "WHATSAPP_SYSTEM_PROMPT_PATH", "app/prompts/whatsapp_system_prompt.txt"
    )
    whatsapp_system_prompt_env: str = _env("WHATSAPP_SYSTEM_PROMPT", "")

    # Media
    whatsapp_video_vip_pitch: str = _env("WHATSAPP_VIDEO_VIP_PITCH", "")

    # Stripe
    stripe_secret_key: str = _env("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = _env("STRIPE_WEBHOOK_SECRET", "")
    stripe_vip_price_id: str = _env("STRIPE_VIP_PRICE_ID", "")
    stripe_success_url: str = _env("STRIPE_SUCCESS_URL", "")
    stripe_cancel_url: str = _env("STRIPE_CANCEL_URL", "")

    # Automation
    cron_token: str = _env("CRON_TOKEN", "")

    # Event (fallback)
    event_name: str = _env("EVENT_NAME", "Beyond Wealth")
    event_date: str = _env("EVENT_DATE", "")
    event_place: str = _env("EVENT_PLACE", "")
    event_speakers: str = _env("EVENT_SPEAKERS", "")
    vip_price: str = _env("VIP_PRICE", "")


settings = Settings()
