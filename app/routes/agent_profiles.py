"""
Agent Profiles — CRUD for agent role assignments per campaign.
Each agent can have multiple profiles (confirmador, setter, closer, etc.)
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from ..settings import settings

logger = logging.getLogger("agent_profiles")

router = APIRouter(prefix="/v1/agent-profiles", tags=["agent-profiles"])

VALID_PROFILES = {"confirmador", "setter", "closer", "seguimiento", "upsell", "lider"}


def _sb():
    from supabase import create_client
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


class CreateProfileRequest(BaseModel):
    campaign_id: str
    user_id: str
    profile_type: str
    config: dict = {}


class UpdateProfileRequest(BaseModel):
    is_active: bool | None = None
    config: dict | None = None


@router.get("")
async def list_profiles(
    campaign_id: str = "",
    user_id: str = "",
    profile_type: str = "",
):
    """List agent profiles. Filter by campaign, user, and/or profile type."""
    sb = _sb()
    q = sb.table("agent_profiles").select("*")
    if campaign_id:
        q = q.eq("campaign_id", campaign_id)
    if user_id:
        q = q.eq("user_id", user_id)
    if profile_type:
        q = q.eq("profile_type", profile_type)
    r = q.order("created_at").execute()
    return {"ok": True, "data": r.data or []}


@router.get("/my")
async def my_profiles(campaign_id: str, user_id: str):
    """Get the current user's active profiles for a campaign."""
    sb = _sb()
    r = (
        sb.table("agent_profiles")
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    )
    return {"ok": True, "data": r.data or []}


@router.post("")
async def create_profile(body: CreateProfileRequest):
    """Assign a profile to an agent for a campaign."""
    if body.profile_type not in VALID_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid profile_type. Must be one of: {', '.join(sorted(VALID_PROFILES))}",
        )

    sb = _sb()

    # Check if already exists
    existing = (
        sb.table("agent_profiles")
        .select("id")
        .eq("campaign_id", body.campaign_id)
        .eq("user_id", body.user_id)
        .eq("profile_type", body.profile_type)
        .limit(1)
        .execute()
    )
    if existing.data:
        # Reactivate if it was deactivated
        sb.table("agent_profiles").update({"is_active": True, "config": body.config}).eq(
            "id", existing.data[0]["id"]
        ).execute()
        return {"ok": True, "data": existing.data[0], "reactivated": True}

    r = (
        sb.table("agent_profiles")
        .insert(
            {
                "campaign_id": body.campaign_id,
                "user_id": body.user_id,
                "profile_type": body.profile_type,
                "config": body.config,
            }
        )
        .execute()
    )
    logger.info(
        "profile_created campaign=%s user=%s type=%s",
        body.campaign_id,
        body.user_id,
        body.profile_type,
    )
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.patch("/{profile_id}")
async def update_profile(profile_id: str, body: UpdateProfileRequest):
    """Update a profile (activate/deactivate, change config)."""
    sb = _sb()
    updates = {}
    if body.is_active is not None:
        updates["is_active"] = body.is_active
    if body.config is not None:
        updates["config"] = body.config
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    r = sb.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return {"ok": True, "data": (r.data or [{}])[0]}


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str):
    """Remove a profile assignment (soft delete — sets is_active=false)."""
    sb = _sb()
    r = (
        sb.table("agent_profiles")
        .update({"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", profile_id)
        .execute()
    )
    return {"ok": True}


@router.get("/team/{campaign_id}")
async def campaign_team(campaign_id: str):
    """Get all team members (campaign_members) for a campaign with their user info."""
    import httpx
    sb = _sb()
    r = sb.table("campaign_members").select("user_id").eq("campaign_id", campaign_id).execute()
    members = r.data or []

    result = []
    for m in members:
        uid = m["user_id"]
        email = ""
        name = ""
        role = "agent"
        try:
            # Get email from Supabase GoTrue admin API
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{settings.supabase_url}/auth/v1/admin/users/{uid}",
                    headers={
                        "apikey": settings.supabase_service_role_key,
                        "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    },
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    email = user_data.get("email", "")
                    meta = user_data.get("user_metadata", {})
                    name = meta.get("full_name", "") or meta.get("name", "") or email.split("@")[0]
        except Exception:
            pass

        # Get role from org_members
        try:
            ur = sb.table("org_members").select("role").eq("user_id", uid).limit(1).execute()
            if ur.data:
                role = ur.data[0].get("role", "agent")
        except Exception:
            pass

        result.append({
            "user_id": uid,
            "role": role,
            "email": email,
            "name": name,
        })

    return {"ok": True, "data": result}


@router.get("/types")
async def list_profile_types():
    """Return available profile types with descriptions."""
    return {
        "ok": True,
        "data": [
            {
                "type": "confirmador",
                "name": "Spartan Confirmador",
                "description": "Llamadas rápidas para confirmar asistencia a eventos",
                "icon": "📞",
            },
            {
                "type": "setter",
                "name": "Setter",
                "description": "Agenda citas con closers vía llamada",
                "icon": "📅",
            },
            {
                "type": "closer",
                "name": "Closer",
                "description": "Cierra ventas por Zoom o llamada",
                "icon": "🎯",
            },
            {
                "type": "seguimiento",
                "name": "Seguimiento",
                "description": "Follow-up con asistentes e interesados para cerrar ventas",
                "icon": "🔄",
            },
            {
                "type": "upsell",
                "name": "Upsell",
                "description": "Ofrece productos de mayor valor a compradores existentes",
                "icon": "💎",
            },
            {
                "type": "lider",
                "name": "Líder de Proyecto",
                "description": "Coordina la campaña y monitorea métricas",
                "icon": "👑",
            },
        ],
    }
