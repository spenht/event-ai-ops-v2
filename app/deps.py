from __future__ import annotations

from supabase import create_client, Client

from .settings import settings


def get_supabase() -> Client:
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("Missing SUPABASE_URL / SUPABASE_KEY")
    return create_client(settings.supabase_url, settings.supabase_key)


sb = get_supabase()
