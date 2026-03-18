from __future__ import annotations

import base64
import json
import logging
from typing import Any

import anyio
import httpx

from ..settings import settings

logger = logging.getLogger("telnyx_calls")

TELNYX_API_BASE = "https://api.telnyx.com/v2"


# ─── Per-campaign credential extraction ─────────────────────────────────────

def _campaign_telnyx(campaign: dict | None) -> dict:
    """Extract Telnyx credentials from campaign for per-campaign overrides."""
    if not campaign:
        return {}
    creds = {}
    api_key = (campaign.get("telnyx_api_key") or "").strip()
    conn_id = (campaign.get("telnyx_sip_connection_id") or "").strip()
    from_num = (campaign.get("telnyx_from_number") or "").strip()
    if api_key:
        creds["telnyx_api_key"] = api_key
    if conn_id:
        creds["connection_id"] = conn_id
    if from_num:
        creds["from_number"] = from_num
    return creds


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _telnyx_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _resolve_api_key(api_key: str) -> str:
    """Return per-campaign key if provided, otherwise fall back to settings."""
    key = api_key or settings.telnyx_api_key
    if not key:
        raise RuntimeError("Missing TELNYX_API_KEY")
    return key


def _encode_client_state(data: dict) -> str:
    """Base64-encode a dict as JSON for Telnyx client_state."""
    return base64.b64encode(json.dumps(data).encode()).decode()


# ─── Generic call control action ────────────────────────────────────────────

def _telnyx_call_control_sync(
    call_control_id: str,
    action: str,
    api_key: str,
    payload: dict | None = None,
) -> dict:
    """Synchronous POST to a Telnyx Call Control action endpoint.

    POST https://api.telnyx.com/v2/calls/{call_control_id}/actions/{action}
    Returns parsed JSON response.
    """
    url = f"{TELNYX_API_BASE}/calls/{call_control_id}/actions/{action}"
    headers = _telnyx_headers(api_key)
    body = payload or {}

    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "%s_failed status=%s body=%s", action, r.status_code, r.text[:1200]
            )
            r.raise_for_status()
        data = r.json()
        logger.info("%s_ok call=%s", action, call_control_id)
        return data


# ─── Dial outbound ───────────────────────────────────────────────────────────

def _dial_outbound_sync(
    to_number: str,
    from_number: str,
    connection_id: str,
    api_key: str,
    webhook_url: str,
    client_state: str,
    record: str,
    amd: str = "detect",
) -> dict:
    """Synchronous outbound dial — runs inside a worker thread."""
    url = f"{TELNYX_API_BASE}/calls"
    headers = _telnyx_headers(api_key)
    body: dict[str, Any] = {
        "connection_id": connection_id,
        "to": to_number,
        "from": from_number,
    }
    if amd:
        body["answering_machine_detection"] = amd
    if webhook_url:
        body["webhook_url"] = webhook_url
    if record:
        body["record"] = record
    if client_state:
        body["client_state"] = client_state

    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "dial_outbound_failed status=%s body=%s", r.status_code, r.text[:1200]
            )
            r.raise_for_status()
        data = r.json().get("data", {})
        result = {
            "call_control_id": data.get("call_control_id", ""),
            "call_leg_id": data.get("call_leg_id", ""),
            "call_session_id": data.get("call_session_id", ""),
        }
        logger.info(
            "dial_outbound_ok to=%s call_control_id=%s",
            to_number,
            result["call_control_id"],
        )
        return result


async def dial_outbound(
    *,
    to_number: str,
    from_number: str,
    connection_id: str,
    telnyx_api_key: str = "",
    webhook_url: str = "",
    client_state: str = "",
    record: str = "record-from-answer",
    amd: str = "detect",
) -> dict:
    """Dial an outbound call via Telnyx Call Control API.

    Per-campaign credentials: if telnyx_api_key is provided it overrides the
    global settings. Otherwise falls back to env-var settings.

    amd: "detect" for spartan calls, "" (disabled) for AI calls.

    Returns dict with call_control_id, call_leg_id, call_session_id.
    """
    api_key = _resolve_api_key(telnyx_api_key)

    return await anyio.to_thread.run_sync(
        lambda: _dial_outbound_sync(
            to_number, from_number, connection_id, api_key,
            webhook_url, client_state, record, amd,
        )
    )


# ─── Hangup ──────────────────────────────────────────────────────────────────

async def hangup_call(call_control_id: str, telnyx_api_key: str = "") -> bool:
    """Hang up a call."""
    api_key = _resolve_api_key(telnyx_api_key)
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(call_control_id, "hangup", api_key)
        )
        return True
    except Exception as exc:
        logger.error("hangup_failed call=%s err=%s", call_control_id, str(exc)[:300])
        return False


# ─── Answer ──────────────────────────────────────────────────────────────────

async def answer_call(call_control_id: str, telnyx_api_key: str = "") -> bool:
    """Answer an incoming call."""
    api_key = _resolve_api_key(telnyx_api_key)
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(call_control_id, "answer", api_key)
        )
        return True
    except Exception as exc:
        logger.error("answer_failed call=%s err=%s", call_control_id, str(exc)[:300])
        return False


# ─── Media streaming ────────────────────────────────────────────────────────

async def start_media_streaming(
    call_control_id: str,
    telnyx_api_key: str = "",
    stream_url: str = "",
) -> bool:
    """Start bi-directional media streaming on a call."""
    api_key = _resolve_api_key(telnyx_api_key)
    payload = {
        "stream_url": stream_url,
        "stream_track": "both_tracks",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU",
    }
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(
                call_control_id, "streaming_start", api_key, payload
            )
        )
        return True
    except Exception as exc:
        logger.error(
            "streaming_start_failed call=%s err=%s", call_control_id, str(exc)[:300]
        )
        return False


async def stop_media_streaming(
    call_control_id: str, telnyx_api_key: str = ""
) -> bool:
    """Stop media streaming on a call."""
    api_key = _resolve_api_key(telnyx_api_key)
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(
                call_control_id, "streaming_stop", api_key
            )
        )
        return True
    except Exception as exc:
        logger.error(
            "streaming_stop_failed call=%s err=%s", call_control_id, str(exc)[:300]
        )
        return False


# ─── Recording ───────────────────────────────────────────────────────────────

async def start_recording(
    call_control_id: str,
    telnyx_api_key: str = "",
    channels: str = "dual",
) -> bool:
    """Start recording a call."""
    api_key = _resolve_api_key(telnyx_api_key)
    payload = {"channels": channels, "format": "mp3"}
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(
                call_control_id, "record_start", api_key, payload
            )
        )
        return True
    except Exception as exc:
        logger.error(
            "record_start_failed call=%s err=%s", call_control_id, str(exc)[:300]
        )
        return False


# ─── Play audio ──────────────────────────────────────────────────────────────

async def play_audio(
    call_control_id: str,
    telnyx_api_key: str = "",
    audio_url: str = "",
) -> bool:
    """Play an audio file on a call."""
    api_key = _resolve_api_key(telnyx_api_key)
    payload = {"audio_url": audio_url}
    try:
        await anyio.to_thread.run_sync(
            lambda: _telnyx_call_control_sync(
                call_control_id, "playback_start", api_key, payload
            )
        )
        return True
    except Exception as exc:
        logger.error(
            "playback_start_failed call=%s err=%s", call_control_id, str(exc)[:300]
        )
        return False


# ─── WebRTC credentials ─────────────────────────────────────────────────────

def _generate_webrtc_credential_sync(api_key: str, connection_id: str) -> dict:
    """Synchronous WebRTC credential creation — runs inside a worker thread."""
    url = f"{TELNYX_API_BASE}/telephony_credentials"
    headers = _telnyx_headers(api_key)
    body = {"connection_id": connection_id}

    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            logger.error(
                "webrtc_credential_failed status=%s body=%s",
                r.status_code,
                r.text[:1200],
            )
            r.raise_for_status()
        data = r.json().get("data", {})
        result = {
            "id": data.get("id", ""),
            "sip_username": data.get("sip_username", ""),
            "sip_password": data.get("sip_password", ""),
        }
        logger.info("webrtc_credential_ok id=%s", result["id"])
        return result


async def generate_webrtc_credential(
    *,
    telnyx_api_key: str = "",
    connection_id: str = "",
) -> dict:
    """Create a WebRTC credential for browser-based calling.

    POST https://api.telnyx.com/v2/telephony_credentials
    Returns dict with id, sip_username, sip_password.
    """
    api_key = _resolve_api_key(telnyx_api_key)
    conn_id = connection_id or settings.telnyx_sip_connection_id
    if not conn_id:
        raise RuntimeError("Missing TELNYX_SIP_CONNECTION_ID")

    return await anyio.to_thread.run_sync(
        lambda: _generate_webrtc_credential_sync(api_key, conn_id)
    )
