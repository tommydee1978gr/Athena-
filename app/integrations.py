from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import ssl
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

from .config import MEDIA_DIR, PUBLIC_BASE_URL
from .db import connect, utcnow
from .security import decrypt_json, encrypt_json, token_hash

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_API = "https://open.tiktokapis.com"
# "Instagram API with Instagram Login" — the account-holder connects directly,
# no Facebook Page required. Only works for Professional (Business/Creator)
# Instagram accounts; Instagram has no API access for personal accounts at all.
INSTAGRAM_AUTH_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_LONG_LIVED_URL = "https://graph.instagram.com/access_token"
INSTAGRAM_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
INSTAGRAM_API = "https://graph.instagram.com"

GOOGLE_SCOPE_PRESETS: dict[str, dict[str, list[str]]] = {
    "gmail": {
        "read": ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly"],
        "write": ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"],
    },
    "calendar": {
        "read": ["openid", "email", "profile", "https://www.googleapis.com/auth/calendar.readonly"],
        "write": ["openid", "email", "profile", "https://www.googleapis.com/auth/calendar.events"],
    },
    "tasks": {
        "read": ["openid", "email", "profile", "https://www.googleapis.com/auth/tasks.readonly"],
        "write": ["openid", "email", "profile", "https://www.googleapis.com/auth/tasks"],
    },
    "youtube": {
        "read": ["openid", "email", "profile", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"],
        "write": ["openid", "email", "profile", "https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/yt-analytics.readonly"],
    },
}

SPOTIFY_SCOPES = [
    "user-read-private", "user-read-email", "user-read-playback-state", "user-modify-playback-state",
    "playlist-read-private", "playlist-modify-private", "playlist-modify-public", "user-library-read", "user-library-modify",
]
TIKTOK_SCOPES = ["user.info.basic", "video.list", "video.upload", "video.publish"]
INSTAGRAM_SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]


def _pkce_pair() -> tuple[str, str]:
    """Return an RFC 7636 verifier and S256 challenge."""
    verifier = secrets.token_urlsafe(72)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class IntegrationError(RuntimeError):
    def __init__(self, status: str, message: str, *, http_status: int = 502, details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.http_status = http_status
        self.details = details


def safe_media_path(raw: str, *, must_exist: bool = True) -> Path:
    path = (MEDIA_DIR / raw.lstrip("/")).resolve()
    root = MEDIA_DIR.resolve()
    if path != root and root not in path.parents:
        raise IntegrationError("permission_denied", "Invalid media path", http_status=400)
    if must_exist and not path.is_file():
        raise IntegrationError("not_found", "Media file not found", http_status=404)
    return path




def safe_user_media_path(user_id: str, raw: str) -> Path:
    path = safe_media_path(raw)
    relative = path.relative_to(MEDIA_DIR.resolve()).as_posix()
    with connect() as conn:
        row = conn.execute("SELECT owner_id FROM media_files WHERE relative_path=?", (relative,)).fetchone()
    if not row or row["owner_id"] != user_id:
        raise IntegrationError("permission_denied", "Media file is not owned by the current user", http_status=403)
    return path

def get_app_config(provider: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT value_enc FROM app_credentials WHERE provider=?", (provider,)).fetchone()
    return decrypt_json(row["value_enc"]) if row else None


def set_app_config(provider: str, value: dict[str, Any], configured_by: str) -> None:
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_credentials(provider,value_enc,configured_by,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(provider) DO UPDATE SET value_enc=excluded.value_enc,configured_by=excluded.configured_by,updated_at=excluded.updated_at""",
            (provider, encrypt_json(value), configured_by, now),
        )


def get_system_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value_enc FROM system_settings WHERE key=?", (key,)).fetchone()
    return decrypt_json(row["value_enc"]) if row else default


def set_system_setting(key: str, value: Any, updated_by: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO system_settings(key,value_enc,updated_by,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (key, encrypt_json(value), updated_by, utcnow()),
        )


def get_connection(user_id: str, provider: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM connections WHERE user_id=? AND provider=?", (user_id, provider)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["token"] = decrypt_json(row["token_enc"])
    result.pop("token_enc", None)
    return result


def save_connection(user_id: str, provider: str, token: dict[str, Any], scopes: Iterable[str], account_label: str = "") -> None:
    now = datetime.now(timezone.utc)
    expires_at = None
    if token.get("expires_in"):
        expires_at = (now + timedelta(seconds=max(0, int(token["expires_in"]) - 30))).isoformat()
    existing = get_connection(user_id, provider)
    if existing and not token.get("refresh_token") and existing["token"].get("refresh_token"):
        token["refresh_token"] = existing["token"]["refresh_token"]
    with connect() as conn:
        conn.execute(
            """INSERT INTO connections(id,user_id,provider,account_label,token_enc,scopes,expires_at,status,last_error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id,provider) DO UPDATE SET account_label=excluded.account_label,token_enc=excluded.token_enc,
               scopes=excluded.scopes,expires_at=excluded.expires_at,status='connected',last_error='',updated_at=excluded.updated_at""",
            (str(uuid.uuid4()), user_id, provider, account_label, encrypt_json(token), " ".join(sorted(set(scopes))), expires_at, "connected", "", now.isoformat(), now.isoformat()),
        )


def disconnect(user_id: str, provider: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM connections WHERE user_id=? AND provider=?", (user_id, provider))


def update_connection_status(user_id: str, provider: str, status: str, error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE connections SET status=?,last_error=?,updated_at=? WHERE user_id=? AND provider=?",
            (status, error[:2000], utcnow(), user_id, provider),
        )


def connection_status(user_id: str, provider: str, *, app_provider: str | None = None) -> dict[str, Any]:
    app_provider = app_provider or provider
    cfg = get_app_config(app_provider) or {}
    required = {
        "google": ("client_id", "client_secret"),
        "spotify": ("client_id",),
        "tiktok": ("client_key", "client_secret"),
        "instagram": ("client_id", "client_secret"),
    }.get(app_provider, ())
    if not cfg or not all(cfg.get(field) for field in required):
        return {"provider": provider, "status": "not_configured"}
    conn = get_connection(user_id, provider)
    if not conn:
        return {"provider": provider, "status": "authorization_required"}
    if conn.get("status") != "connected":
        return {"provider": provider, "status": conn.get("status", "error"), "error": conn.get("last_error", "")}
    expired = bool(conn.get("expires_at") and conn["expires_at"] <= datetime.now(timezone.utc).isoformat())
    refreshable = bool(conn.get("token", {}).get("refresh_token"))
    return {
        "provider": provider,
        "status": "connected" if not expired or refreshable else "token_expired",
        "account": conn.get("account_label", ""),
        "scopes": conn.get("scopes", ""),
        "expires_at": conn.get("expires_at"),
        "refreshable": refreshable,
    }


def require_connection_scopes(user_id: str, provider: str, required: Iterable[str]) -> None:
    conn = get_connection(user_id, provider)
    if not conn:
        raise IntegrationError("authorization_required", f"{provider} is not connected", http_status=401)
    granted = set(str(conn.get("scopes", "")).replace(",", " ").split())
    missing = sorted(set(required) - granted)
    if missing:
        raise IntegrationError(
            "permission_denied",
            f"{provider} is connected without required OAuth scopes",
            http_status=403,
            details={"missing_scopes": missing, "granted_scopes": sorted(granted)},
        )


def public_base_url(request_base: str) -> str:
    configured = str(get_system_setting("public_base_url", "") or "").strip().rstrip("/")
    return PUBLIC_BASE_URL or configured or request_base.rstrip("/")


def store_oauth_state(user_id: str, provider: str, redirect_uri: str, requested_scopes: list[str], state: str, verifier: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO oauth_states(state_hash,user_id,provider,redirect_uri,verifier_enc,requested_scopes,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (token_hash(state), user_id, provider, redirect_uri, encrypt_json({"verifier": verifier}) if verifier else None,
             " ".join(requested_scopes), (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), utcnow()),
        )


def consume_oauth_state(state: str, provider: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state_hash=? AND provider=? AND expires_at>?",
            (token_hash(state), provider, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if not row:
            raise IntegrationError("permission_denied", "OAuth state is invalid or expired", http_status=400)
        conn.execute("DELETE FROM oauth_states WHERE state_hash=?", (row["state_hash"],))
    result = dict(row)
    if row["verifier_enc"]:
        result.update(decrypt_json(row["verifier_enc"]))
    return result


async def _json_request(method: str, url: str, *, headers: dict[str, str] | None = None, timeout: float = 30, **kwargs) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:1000]}
    if response.status_code >= 400:
        status = "permission_denied" if response.status_code in (401, 403) else "error"
        raise IntegrationError(status, f"Provider returned HTTP {response.status_code}", http_status=502, details=data)
    return data


async def google_authorization_url(user_id: str, service: str, mode: str, request_base: str, slot: str = "1") -> str:
    cfg = get_app_config("google")
    if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise IntegrationError("not_configured", "Google OAuth application is not configured", http_status=409)
    if service not in GOOGLE_SCOPE_PRESETS or mode not in GOOGLE_SCOPE_PRESETS[service]:
        raise IntegrationError("invalid_request", "Unsupported Google service or permission mode", http_status=400)
    # slot lets the same person hold more than one Google account connected for the
    # same service (e.g. personal + work Gmail) without the second overwriting the
    # first — each slot is just a distinct `provider` string, no schema change needed.
    slot = slot if slot.isalnum() and len(slot) <= 8 else "1"
    provider = f"google_{service}" if slot == "1" else f"google_{service}_{slot}"
    redirect_uri = f"{public_base_url(request_base)}/oauth/google/callback"
    scopes = GOOGLE_SCOPE_PRESETS[service][mode]
    state = uuid.uuid4().hex + uuid.uuid4().hex
    verifier, challenge = _pkce_pair()
    store_oauth_state(user_id, provider, redirect_uri, scopes, state, verifier)
    params = {
        "client_id": cfg["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "include_granted_scopes": "false", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def google_callback(code: str, state: str) -> tuple[str, str]:
    # State can belong to any granular Google connector.
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state_hash=? AND provider LIKE 'google_%' AND expires_at>?",
            (token_hash(state), datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if not row:
            raise IntegrationError("permission_denied", "OAuth state is invalid or expired", http_status=400)
        conn.execute("DELETE FROM oauth_states WHERE state_hash=?", (row["state_hash"],))
    state_data = dict(row)
    if row["verifier_enc"]:
        state_data.update(decrypt_json(row["verifier_enc"]))
    cfg = get_app_config("google") or {}
    token = await _json_request("POST", GOOGLE_TOKEN_URL, data={
        "code": code, "client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", ""),
        "redirect_uri": state_data["redirect_uri"], "grant_type": "authorization_code", "code_verifier": state_data.get("verifier", ""),
    })
    userinfo = await _json_request("GET", GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token['access_token']}"})
    scopes = token.get("scope", state_data["requested_scopes"]).split()
    save_connection(state_data["user_id"], state_data["provider"], token, scopes, userinfo.get("email", userinfo.get("name", "Google")))
    return state_data["user_id"], state_data["provider"]


async def _refresh_google(user_id: str, provider: str, conn: dict[str, Any]) -> dict[str, Any]:
    token = conn["token"]
    if not token.get("refresh_token"):
        raise IntegrationError("authorization_required", "Google refresh token is missing", http_status=401)
    cfg = get_app_config("google") or {}
    refreshed = await _json_request("POST", GOOGLE_TOKEN_URL, data={
        "client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", ""),
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    })
    refreshed["refresh_token"] = token["refresh_token"]
    save_connection(user_id, provider, refreshed, conn["scopes"].split(), conn.get("account_label", ""))
    return refreshed


async def access_token(user_id: str, provider: str) -> str:
    conn = get_connection(user_id, provider)
    if not conn:
        raise IntegrationError("authorization_required", f"{provider} is not connected", http_status=401)
    token = conn["token"]
    expires_at = conn.get("expires_at")
    if expires_at and expires_at <= datetime.now(timezone.utc).isoformat():
        try:
            if provider.startswith("google_"):
                token = await _refresh_google(user_id, provider, conn)
            elif provider == "spotify":
                token = await _refresh_spotify(user_id, conn)
            elif provider == "tiktok":
                token = await _refresh_tiktok(user_id, conn)
            elif provider == "instagram":
                token = await _refresh_instagram(user_id, conn)
            else:
                raise IntegrationError("token_expired", f"{provider} token expired", http_status=401)
        except IntegrationError as exc:
            update_connection_status(user_id, provider, exc.status, exc.message)
            raise
    value = token.get("access_token")
    if not value:
        raise IntegrationError("authorization_required", f"{provider} access token is missing", http_status=401)
    return value


async def google_api(user_id: str, provider: str, method: str, url: str, **kwargs) -> dict[str, Any]:
    token = await access_token(user_id, provider)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    return await _json_request(method, url, headers=headers, **kwargs)


async def gmail_list(user_id: str, query: str = "", max_results: int = 20) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_gmail", ["https://www.googleapis.com/auth/gmail.readonly"])
    params: dict[str, Any] = {"maxResults": max(1, min(max_results, 100))}
    if query:
        params["q"] = query
    listing = await google_api(user_id, "google_gmail", "GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", params=params)
    messages = []
    for item in listing.get("messages", [])[:20]:
        msg = await google_api(user_id, "google_gmail", "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{item['id']}", params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({"id": msg.get("id"), "threadId": msg.get("threadId"), "snippet": msg.get("snippet", ""), "headers": headers})
    return {"messages": messages, "resultSizeEstimate": listing.get("resultSizeEstimate", len(messages))}


async def gmail_get(user_id: str, message_id: str, format_name: str = "full") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_gmail", ["https://www.googleapis.com/auth/gmail.readonly"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", message_id):
        raise IntegrationError("invalid_request", "Invalid Gmail message id", http_status=400)
    if format_name not in {"full", "metadata", "minimal", "raw"}:
        raise IntegrationError("invalid_request", "Invalid Gmail message format", http_status=400)
    return await google_api(user_id, "google_gmail", "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}", params={"format": format_name})


async def gmail_send(user_id: str, to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_gmail", ["https://www.googleapis.com/auth/gmail.send"])
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    return await google_api(user_id, "google_gmail", "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", json={"raw": raw})


async def calendar_list(user_id: str, time_min: str | None = None, max_results: int = 20) -> dict[str, Any]:
    conn = get_connection(user_id, "google_calendar")
    granted = set(str(conn.get("scopes", "")).split()) if conn else set()
    if not ({"https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events"} & granted):
        require_connection_scopes(user_id, "google_calendar", ["https://www.googleapis.com/auth/calendar.readonly"])
    params: dict[str, Any] = {"singleEvents": "true", "orderBy": "startTime", "maxResults": max(1, min(max_results, 100))}
    params["timeMin"] = time_min or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return await google_api(user_id, "google_calendar", "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events", params=params)


async def calendar_create(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_calendar", ["https://www.googleapis.com/auth/calendar.events"])
    return await google_api(user_id, "google_calendar", "POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", json=payload)


async def calendar_update(user_id: str, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_calendar", ["https://www.googleapis.com/auth/calendar.events"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", event_id):
        raise IntegrationError("invalid_request", "Invalid Calendar event id", http_status=400)
    return await google_api(user_id, "google_calendar", "PATCH", f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}", json=payload)


async def calendar_delete(user_id: str, event_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_calendar", ["https://www.googleapis.com/auth/calendar.events"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", event_id):
        raise IntegrationError("invalid_request", "Invalid Calendar event id", http_status=400)
    token = await access_token(user_id, "google_calendar")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}", headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc
    if response.status_code >= 400:
        raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Google Calendar returned HTTP {response.status_code}", details=_safe_response(response))
    return {"status": "deleted", "provider_http_status": response.status_code, "event_id": event_id}


async def tasks_list(user_id: str, tasklist: str = "@default", max_results: int = 50) -> dict[str, Any]:
    conn = get_connection(user_id, "google_tasks")
    granted = set(str(conn.get("scopes", "")).split()) if conn else set()
    if not ({"https://www.googleapis.com/auth/tasks.readonly", "https://www.googleapis.com/auth/tasks"} & granted):
        require_connection_scopes(user_id, "google_tasks", ["https://www.googleapis.com/auth/tasks.readonly"])
    return await google_api(user_id, "google_tasks", "GET", f"https://tasks.googleapis.com/tasks/v1/lists/{tasklist}/tasks", params={"maxResults": max(1, min(max_results, 100)), "showCompleted": "true"})


async def tasks_create(user_id: str, title: str, notes: str = "", due: str | None = None, tasklist: str = "@default") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_tasks", ["https://www.googleapis.com/auth/tasks"])
    body: dict[str, Any] = {"title": title, "notes": notes}
    if due:
        body["due"] = due
    return await google_api(user_id, "google_tasks", "POST", f"https://tasks.googleapis.com/tasks/v1/lists/{tasklist}/tasks", json=body)


async def tasks_update(user_id: str, task_id: str, payload: dict[str, Any], tasklist: str = "@default") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_tasks", ["https://www.googleapis.com/auth/tasks"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", task_id):
        raise IntegrationError("invalid_request", "Invalid Google Task id", http_status=400)
    return await google_api(user_id, "google_tasks", "PATCH", f"https://tasks.googleapis.com/tasks/v1/lists/{tasklist}/tasks/{task_id}", json=payload)


async def tasks_delete(user_id: str, task_id: str, tasklist: str = "@default") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_tasks", ["https://www.googleapis.com/auth/tasks"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", task_id):
        raise IntegrationError("invalid_request", "Invalid Google Task id", http_status=400)
    token = await access_token(user_id, "google_tasks")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(f"https://tasks.googleapis.com/tasks/v1/lists/{tasklist}/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc
    if response.status_code >= 400:
        raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Google Tasks returned HTTP {response.status_code}", details=_safe_response(response))
    return {"status": "deleted", "provider_http_status": response.status_code, "task_id": task_id}


async def youtube_channel(user_id: str) -> dict[str, Any]:
    conn = get_connection(user_id, "google_youtube")
    granted = set(str(conn.get("scopes", "")).split()) if conn else set()
    if not ({"https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl"} & granted):
        require_connection_scopes(user_id, "google_youtube", ["https://www.googleapis.com/auth/youtube.readonly"])
    return await google_api(user_id, "google_youtube", "GET", "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet,statistics,contentDetails", "mine": "true"})


async def youtube_comments(user_id: str, video_id: str, max_results: int = 50) -> dict[str, Any]:
    conn = get_connection(user_id, "google_youtube")
    granted = set(str(conn.get("scopes", "")).split()) if conn else set()
    if not ({"https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl"} & granted):
        require_connection_scopes(user_id, "google_youtube", ["https://www.googleapis.com/auth/youtube.readonly"])
    return await google_api(user_id, "google_youtube", "GET", "https://www.googleapis.com/youtube/v3/commentThreads", params={"part": "snippet,replies", "videoId": video_id, "maxResults": max(1, min(max_results, 100)), "textFormat": "plainText"})


async def youtube_reply_comment(user_id: str, parent_id: str, text: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_youtube", ["https://www.googleapis.com/auth/youtube.force-ssl"])
    if not parent_id or len(parent_id) > 256:
        raise IntegrationError("invalid_request", "Invalid YouTube parent comment id", http_status=400)
    return await google_api(user_id, "google_youtube", "POST", "https://www.googleapis.com/youtube/v3/comments", params={"part": "snippet"}, json={"snippet": {"parentId": parent_id, "textOriginal": text}})


async def youtube_analytics(user_id: str, start_date: str, end_date: str, metrics: str = "views,estimatedMinutesWatched,subscribersGained", dimensions: str = "day") -> dict[str, Any]:
    require_connection_scopes(user_id, "google_youtube", ["https://www.googleapis.com/auth/yt-analytics.readonly"])
    return await google_api(user_id, "google_youtube", "GET", "https://youtubeanalytics.googleapis.com/v2/reports", params={
        "ids": "channel==MINE", "startDate": start_date, "endDate": end_date, "metrics": metrics, "dimensions": dimensions, "sort": dimensions,
    })


async def youtube_upload(user_id: str, media_path: str, title: str, description: str = "", privacy_status: str = "private", category_id: str = "22", tags: list[str] | None = None) -> dict[str, Any]:
    require_connection_scopes(user_id, "google_youtube", ["https://www.googleapis.com/auth/youtube.upload"])
    path = safe_user_media_path(user_id, media_path)
    token = await access_token(user_id, "google_youtube")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    metadata = {"snippet": {"title": title, "description": description, "categoryId": category_id, "tags": tags or []}, "status": {"privacyStatus": privacy_status}}
    headers = {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(path.stat().st_size), "X-Upload-Content-Type": mime,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            init = await client.post("https://www.googleapis.com/upload/youtube/v3/videos", params={"uploadType": "resumable", "part": "snippet,status"}, headers=headers, json=metadata)
            if init.status_code >= 400:
                raise IntegrationError("error", "YouTube upload initialization failed", details=_safe_response(init))
            upload_url = init.headers.get("location")
            if not upload_url:
                raise IntegrationError("error", "YouTube did not return a resumable upload URL")
            with path.open("rb") as handle:
                upload = await client.put(upload_url, headers={"Authorization": f"Bearer {token}", "Content-Type": mime, "Content-Length": str(path.stat().st_size)}, content=handle)
            if upload.status_code >= 400:
                raise IntegrationError("error", "YouTube media upload failed", details=_safe_response(upload))
            return upload.json()
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


def _safe_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:1000]


async def spotify_authorization_url(user_id: str, request_base: str) -> str:
    cfg = get_app_config("spotify")
    if not cfg or not cfg.get("client_id"):
        raise IntegrationError("not_configured", "Spotify application is not configured", http_status=409)
    redirect_uri = f"{public_base_url(request_base)}/oauth/spotify/callback"
    state = uuid.uuid4().hex + uuid.uuid4().hex
    verifier, challenge = _pkce_pair()
    store_oauth_state(user_id, "spotify", redirect_uri, SPOTIFY_SCOPES, state, verifier)
    return f"{SPOTIFY_AUTH_URL}?{urlencode({'client_id': cfg['client_id'], 'response_type': 'code', 'redirect_uri': redirect_uri, 'scope': ' '.join(SPOTIFY_SCOPES), 'state': state, 'show_dialog': 'true', 'code_challenge_method': 'S256', 'code_challenge': challenge})}"


async def spotify_callback(code: str, state: str) -> tuple[str, str]:
    state_data = consume_oauth_state(state, "spotify")
    cfg = get_app_config("spotify") or {}
    token = await _json_request("POST", SPOTIFY_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": state_data["redirect_uri"],
        "client_id": cfg.get("client_id", ""), "code_verifier": state_data.get("verifier", ""),
    })
    profile = await _json_request("GET", f"{SPOTIFY_API}/me", headers={"Authorization": f"Bearer {token['access_token']}"})
    scopes = token.get("scope", state_data["requested_scopes"]).split()
    save_connection(state_data["user_id"], "spotify", token, scopes, profile.get("display_name") or profile.get("id", "Spotify"))
    return state_data["user_id"], "spotify"


async def _refresh_spotify(user_id: str, conn: dict[str, Any]) -> dict[str, Any]:
    cfg = get_app_config("spotify") or {}
    refresh = conn["token"].get("refresh_token")
    if not refresh:
        raise IntegrationError("authorization_required", "Spotify refresh token is missing", http_status=401)
    token = await _json_request("POST", SPOTIFY_TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cfg.get("client_id", "")})
    token["refresh_token"] = token.get("refresh_token") or refresh
    save_connection(user_id, "spotify", token, conn["scopes"].split(), conn.get("account_label", ""))
    return token


async def spotify_api(user_id: str, method: str, path: str, **kwargs) -> dict[str, Any]:
    token = await access_token(user_id, "spotify")
    url = path if path.startswith("http") else f"{SPOTIFY_API}{path}"
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    if method.upper() in {"PUT", "DELETE"}:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code >= 400:
                raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Spotify returned HTTP {response.status_code}", details=_safe_response(response))
            return response.json() if response.content else {"status": "ok"}
        except httpx.RequestError as exc:
            raise IntegrationError("provider_unavailable", str(exc)) from exc
    return await _json_request(method, url, headers=headers, **kwargs)


async def spotify_profile(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-read-private"])
    return await spotify_api(user_id, "GET", "/me")


async def spotify_playlists(user_id: str, limit: int = 50) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["playlist-read-private"])
    return await spotify_api(user_id, "GET", "/me/playlists", params={"limit": max(1, min(limit, 50))})


async def spotify_current(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-read-playback-state"])
    return await spotify_api(user_id, "GET", "/me/player/currently-playing")


async def spotify_devices(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-read-playback-state"])
    return await spotify_api(user_id, "GET", "/me/player/devices")


async def spotify_saved_tracks(user_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-library-read"])
    return await spotify_api(user_id, "GET", "/me/tracks", params={"limit": max(1, min(limit, 50)), "offset": max(0, offset)})


async def spotify_save_tracks(user_id: str, track_ids: list[str], remove: bool = False) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-library-modify"])
    ids = [track_id for track_id in track_ids if re.fullmatch(r"[A-Za-z0-9]{22}", track_id)]
    if not ids or len(ids) > 50 or len(ids) != len(track_ids):
        raise IntegrationError("invalid_request", "Spotify track ids must be a list of 1 to 50 valid ids", http_status=400)
    return await spotify_api(user_id, "DELETE" if remove else "PUT", "/me/tracks", json={"ids": ids})


async def spotify_playback(user_id: str, action: str, device_id: str | None = None, uri: str | None = None) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["user-modify-playback-state"])
    params = {"device_id": device_id} if device_id else None
    if action == "play":
        body = {"uris": [uri]} if uri else None
        return await spotify_api(user_id, "PUT", "/me/player/play", params=params, json=body)
    if action == "pause":
        return await spotify_api(user_id, "PUT", "/me/player/pause", params=params)
    if action in {"next", "previous"}:
        return await spotify_api(user_id, "POST", f"/me/player/{action}", params=params)
    raise IntegrationError("invalid_request", "Unsupported Spotify playback action", http_status=400)


async def spotify_create_playlist(user_id: str, name: str, description: str = "", public: bool = False) -> dict[str, Any]:
    require_connection_scopes(user_id, "spotify", ["playlist-modify-public" if public else "playlist-modify-private"])
    profile = await spotify_profile(user_id)
    return await spotify_api(user_id, "POST", f"/users/{profile['id']}/playlists", json={"name": name, "description": description, "public": public})


async def tiktok_authorization_url(user_id: str, request_base: str) -> str:
    cfg = get_app_config("tiktok")
    if not cfg or not cfg.get("client_key") or not cfg.get("client_secret"):
        raise IntegrationError("not_configured", "TikTok application is not configured", http_status=409)
    redirect_uri = f"{public_base_url(request_base)}/oauth/tiktok/callback"
    state = uuid.uuid4().hex + uuid.uuid4().hex
    store_oauth_state(user_id, "tiktok", redirect_uri, TIKTOK_SCOPES, state)
    return f"{TIKTOK_AUTH_URL}?{urlencode({'client_key': cfg['client_key'], 'response_type': 'code', 'scope': ','.join(TIKTOK_SCOPES), 'redirect_uri': redirect_uri, 'state': state})}"


async def tiktok_callback(code: str, state: str) -> tuple[str, str]:
    state_data = consume_oauth_state(state, "tiktok")
    cfg = get_app_config("tiktok") or {}
    token = await _json_request("POST", TIKTOK_TOKEN_URL, data={"client_key": cfg.get("client_key", ""), "client_secret": cfg.get("client_secret", ""), "code": code, "grant_type": "authorization_code", "redirect_uri": state_data["redirect_uri"]})
    scopes = str(token.get("scope", state_data["requested_scopes"])).replace(",", " ").split()
    account = token.get("open_id", "TikTok")
    save_connection(state_data["user_id"], "tiktok", token, scopes, account)
    return state_data["user_id"], "tiktok"


async def _refresh_tiktok(user_id: str, conn: dict[str, Any]) -> dict[str, Any]:
    cfg = get_app_config("tiktok") or {}
    refresh = conn["token"].get("refresh_token")
    if not refresh:
        raise IntegrationError("authorization_required", "TikTok refresh token is missing", http_status=401)
    token = await _json_request("POST", TIKTOK_TOKEN_URL, data={"client_key": cfg.get("client_key", ""), "client_secret": cfg.get("client_secret", ""), "grant_type": "refresh_token", "refresh_token": refresh})
    token["refresh_token"] = token.get("refresh_token") or refresh
    save_connection(user_id, "tiktok", token, str(token.get("scope", conn["scopes"])).replace(",", " ").split(), conn.get("account_label", ""))
    return token


async def tiktok_api(user_id: str, method: str, path: str, **kwargs) -> dict[str, Any]:
    token = await access_token(user_id, "tiktok")
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    result = await _json_request(method, f"{TIKTOK_API}{path}", headers=headers, **kwargs)
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict) and error.get("code") not in (None, "", "ok"):
        code = str(error.get("code", ""))
        status = "requires_provider_approval" if ("scope" in code or "unaudited" in code or "audit" in code) else "permission_denied" if code in {"access_token_invalid", "invalid_access_token"} else "error"
        raise IntegrationError(status, error.get("message") or code, details=result)
    return result


async def tiktok_user(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "tiktok", ["user.info.basic"])
    return await tiktok_api(user_id, "GET", "/v2/user/info/", params={"fields": "open_id,union_id,avatar_url,display_name"})


async def tiktok_videos(user_id: str, max_count: int = 20) -> dict[str, Any]:
    require_connection_scopes(user_id, "tiktok", ["video.list"])
    return await tiktok_api(user_id, "POST", "/v2/video/list/?fields=id,title,video_description,duration,cover_image_url,share_url,view_count,like_count,comment_count,share_count", json={"max_count": max(1, min(max_count, 20))})


async def tiktok_creator_info(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "tiktok", ["video.publish"])
    return await tiktok_api(user_id, "POST", "/v2/post/publish/creator_info/query/", json={})


def _chunk_geometry(size: int) -> tuple[int, int]:
    if size <= 0:
        raise IntegrationError("invalid_request", "Video file is empty", http_status=400)
    min_chunk = 5 * 1024 * 1024
    max_chunk = 64 * 1024 * 1024
    if size < min_chunk:
        return size, 1
    count = min(1000, max(1, math.ceil(size / max_chunk)))
    chunk = math.ceil(size / count)
    if count > 1 and chunk < min_chunk:
        chunk = min_chunk
        count = math.ceil(size / chunk)
    return chunk, count


async def _tiktok_upload_file(upload_url: str, path: Path, mime: str, chunk_size: int) -> None:
    total = path.stat().st_size
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            with path.open("rb") as handle:
                start = 0
                while start < total:
                    data = handle.read(chunk_size)
                    if not data:
                        break
                    end = start + len(data) - 1
                    response = await client.put(upload_url, headers={"Content-Type": mime, "Content-Length": str(len(data)), "Content-Range": f"bytes {start}-{end}/{total}"}, content=data)
                    if response.status_code >= 400:
                        raise IntegrationError("error", "TikTok media upload failed", details=_safe_response(response))
                    start = end + 1
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


async def tiktok_publish(user_id: str, media_path: str, *, mode: str, title: str = "", privacy_level: str = "SELF_ONLY", disable_duet: bool = False, disable_comment: bool = False, disable_stitch: bool = False, is_aigc: bool = False) -> dict[str, Any]:
    require_connection_scopes(user_id, "tiktok", ["video.upload" if mode == "draft" else "video.publish"])
    path = safe_user_media_path(user_id, media_path)
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    if mime not in {"video/mp4", "video/quicktime", "video/webm"}:
        raise IntegrationError("invalid_request", "TikTok supports MP4, QuickTime or WebM uploads", http_status=400)
    chunk_size, chunk_count = _chunk_geometry(path.stat().st_size)
    source_info = {"source": "FILE_UPLOAD", "video_size": path.stat().st_size, "chunk_size": chunk_size, "total_chunk_count": chunk_count}
    if mode == "draft":
        result = await tiktok_api(user_id, "POST", "/v2/post/publish/inbox/video/init/", json={"source_info": source_info})
    elif mode == "direct":
        result = await tiktok_api(user_id, "POST", "/v2/post/publish/video/init/", json={
            "post_info": {"title": title, "privacy_level": privacy_level, "disable_duet": disable_duet, "disable_comment": disable_comment, "disable_stitch": disable_stitch, "brand_content_toggle": False, "brand_organic_toggle": False, "is_aigc": is_aigc},
            "source_info": source_info,
        })
    else:
        raise IntegrationError("invalid_request", "TikTok mode must be draft or direct", http_status=400)
    error = result.get("error", {})
    if error and error.get("code") not in (None, "ok"):
        raise IntegrationError("requires_provider_approval" if "scope" in error.get("code", "") or "unaudited" in error.get("code", "") else "error", error.get("message", error.get("code", "TikTok error")), details=result)
    upload_url = result.get("data", {}).get("upload_url")
    if not upload_url:
        raise IntegrationError("error", "TikTok did not return an upload URL", details=result)
    await _tiktok_upload_file(upload_url, path, mime, chunk_size)
    return result


async def tiktok_publish_status(user_id: str, publish_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "tiktok", ["video.publish"])
    return await tiktok_api(user_id, "POST", "/v2/post/publish/status/fetch/", json={"publish_id": publish_id})


async def instagram_authorization_url(user_id: str, request_base: str) -> str:
    cfg = get_app_config("instagram")
    if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise IntegrationError("not_configured", "Instagram application is not configured", http_status=409)
    redirect_uri = f"{public_base_url(request_base)}/oauth/instagram/callback"
    state = uuid.uuid4().hex + uuid.uuid4().hex
    store_oauth_state(user_id, "instagram", redirect_uri, INSTAGRAM_SCOPES, state)
    return f"{INSTAGRAM_AUTH_URL}?{urlencode({'client_id': cfg['client_id'], 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': ','.join(INSTAGRAM_SCOPES), 'state': state})}"


async def instagram_callback(code: str, state: str) -> tuple[str, str]:
    state_data = consume_oauth_state(state, "instagram")
    cfg = get_app_config("instagram") or {}
    # Step 1: exchange the authorization code for a short-lived token (~1h).
    short_lived = await _json_request(
        "POST", INSTAGRAM_TOKEN_URL,
        data={
            "client_id": cfg.get("client_id", ""),
            "client_secret": cfg.get("client_secret", ""),
            "grant_type": "authorization_code",
            "redirect_uri": state_data["redirect_uri"],
            "code": code,
        },
    )
    if not short_lived.get("access_token"):
        raise IntegrationError("error", "Instagram did not return an access token", details=short_lived)
    # Step 2: exchange for a long-lived token (~60 days, refreshable).
    long_lived = await _json_request(
        "GET", INSTAGRAM_LONG_LIVED_URL,
        params={"grant_type": "ig_exchange_token", "client_secret": cfg.get("client_secret", ""), "access_token": short_lived["access_token"]},
    )
    token = {
        "access_token": long_lived.get("access_token", short_lived["access_token"]),
        "token_type": long_lived.get("token_type", "bearer"),
        "expires_in": long_lived.get("expires_in"),
        "obtained_at": utcnow(),
    }
    ig_user_id = short_lived.get("user_id", "")
    profile = await _json_request("GET", f"{INSTAGRAM_API}/me", params={"fields": "username", "access_token": token["access_token"]})
    account = profile.get("username") or f"instagram:{ig_user_id}"
    save_connection(state_data["user_id"], "instagram", token, INSTAGRAM_SCOPES, account)
    return state_data["user_id"], "instagram"


async def _refresh_instagram(user_id: str, conn: dict[str, Any]) -> dict[str, Any]:
    current = conn["token"].get("access_token")
    if not current:
        raise IntegrationError("authorization_required", "Instagram access token is missing", http_status=401)
    # Long-lived tokens refresh themselves — there is no separate refresh_token.
    # Meta requires the token to be at least 24h old and not yet expired.
    refreshed = await _json_request("GET", INSTAGRAM_REFRESH_URL, params={"grant_type": "ig_refresh_token", "access_token": current})
    token = dict(conn["token"])
    token["access_token"] = refreshed.get("access_token", current)
    token["expires_in"] = refreshed.get("expires_in", token.get("expires_in"))
    token["obtained_at"] = utcnow()
    save_connection(user_id, "instagram", token, conn["scopes"], conn.get("account_label", ""))
    return token


async def instagram_api(user_id: str, method: str, path: str, **kwargs) -> dict[str, Any]:
    token = await access_token(user_id, "instagram")
    params = dict(kwargs.pop("params", {}) or {})
    params["access_token"] = token
    result = await _json_request(method, f"{INSTAGRAM_API}{path}", params=params, **kwargs)
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        status = "permission_denied" if code in (190, 10, 200) else "requires_provider_approval" if code == 100 else "error"
        raise IntegrationError(status, error.get("message", "Instagram API error"), details=result)
    return result


async def instagram_profile(user_id: str) -> dict[str, Any]:
    require_connection_scopes(user_id, "instagram", ["instagram_business_basic"])
    return await instagram_api(user_id, "GET", "/me", params={"fields": "id,username,account_type,media_count"})


async def instagram_media(user_id: str, limit: int = 20) -> dict[str, Any]:
    require_connection_scopes(user_id, "instagram", ["instagram_business_basic"])
    return await instagram_api(user_id, "GET", "/me/media", params={"fields": "id,caption,media_type,media_url,permalink,timestamp", "limit": max(1, min(limit, 50))})


async def instagram_publish(user_id: str, media_url: str, caption: str = "", *, is_video: bool = False) -> dict[str, Any]:
    """Publish a photo or Reel. Instagram's API fetches the media itself from a
    URL it can reach server-to-server — it does not accept a raw file upload
    like TikTok does. media_url must be a public, unauthenticated URL (not an
    ATHENA session-authenticated media link). ATHENA does not fabricate one;
    the caller is responsible for a URL Instagram can actually fetch."""
    require_connection_scopes(user_id, "instagram", ["instagram_business_content_publish"])
    if not media_url.startswith("https://"):
        raise IntegrationError("invalid_request", "Instagram requires a public https:// media URL", http_status=400)
    container_fields: dict[str, Any] = {"caption": caption[:2200]}
    container_fields["video_url" if is_video else "image_url"] = media_url
    if is_video:
        container_fields["media_type"] = "REELS"
    container = await instagram_api(user_id, "POST", "/me/media", params=container_fields)
    creation_id = container.get("id")
    if not creation_id:
        raise IntegrationError("error", "Instagram did not return a media container id", details=container)
    if is_video:
        # Reels need processing time; poll status_code until FINISHED (or ERROR).
        for _ in range(30):
            status = await instagram_api(user_id, "GET", f"/{creation_id}", params={"fields": "status_code"})
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise IntegrationError("error", "Instagram failed to process the video", details=status)
            await asyncio.sleep(2)
    return await instagram_api(user_id, "POST", "/me/media_publish", params={"creation_id": creation_id})


async def revoke_connection(user_id: str, provider: str) -> dict[str, Any]:
    """Revoke provider authorization when an official endpoint exists, then delete local tokens."""
    conn = get_connection(user_id, provider)
    if not conn:
        return {"provider": provider, "status": "authorization_required", "local_tokens_deleted": False}
    provider_result = "unsupported_by_provider"
    if provider.startswith("google_"):
        token = conn["token"].get("refresh_token") or conn["token"].get("access_token")
        if token:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(GOOGLE_REVOKE_URL, data={"token": token}, headers={"Content-Type": "application/x-www-form-urlencoded"})
                if response.status_code not in (200, 400):
                    raise IntegrationError("provider_unavailable", f"Google revoke returned HTTP {response.status_code}", details=_safe_response(response))
                provider_result = "revoked" if response.status_code == 200 else "already_invalid_or_revoked"
            except httpx.RequestError as exc:
                raise IntegrationError("provider_unavailable", str(exc)) from exc
    elif provider == "tiktok":
        # TikTok's public web OAuth documentation does not expose a general revoke endpoint.
        provider_result = "unsupported_by_provider"
    elif provider == "spotify":
        # Spotify currently requires removal from the user's Apps page; no Web API revoke endpoint is used here.
        provider_result = "unsupported_by_provider"
    affected = [provider]
    if provider.startswith("google_"):
        with connect() as db:
            rows = db.execute("SELECT provider FROM connections WHERE user_id=? AND provider LIKE 'google_%'", (user_id,)).fetchall()
            affected = [row["provider"] for row in rows]
            db.execute("DELETE FROM connections WHERE user_id=? AND provider LIKE 'google_%'", (user_id,))
    else:
        disconnect(user_id, provider)
    return {"provider": provider, "status": "disconnected", "provider_revoke": provider_result, "local_tokens_deleted": True, "affected_connections": affected}


async def homeassistant_request(method: str, path: str, **kwargs) -> Any:
    cfg = get_app_config("homeassistant")
    if not cfg or not cfg.get("base_url") or not cfg.get("token"):
        raise IntegrationError("not_configured", "Home Assistant is not configured", http_status=409)
    url = f"{cfg['base_url'].rstrip('/')}/{path.lstrip('/')}"
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update({"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Home Assistant returned HTTP {response.status_code}", details=_safe_response(response))
        return response.json() if response.content else {"status": "ok"}
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


async def emby_request(method: str, path: str, **kwargs) -> Any:
    cfg = get_app_config("emby")
    if not cfg or not cfg.get("base_url") or not cfg.get("api_key"):
        raise IntegrationError("not_configured", "Emby is not configured", http_status=409)
    url = f"{cfg['base_url'].rstrip('/')}/{path.lstrip('/')}"
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Emby-Token"] = cfg["api_key"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Emby returned HTTP {response.status_code}", details=_safe_response(response))
        return response.json() if response.content else {"status": "ok"}
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


_omada_token_cache: dict[str, dict[str, Any]] = {}


async def _omada_token() -> tuple[str, str, str]:
    """Client-credentials OAuth against the local Omada Controller — returns
    (access_token, omadac_id, base_url). Cached in-memory per base_url until
    shortly before expiry; the controller's own TLS cert is self-signed
    (local network appliance), hence verify=False, same trust boundary as
    every other integration ATHENA reaches over the LAN.

    Verified end-to-end live against the real controller 2026-08-07 (token
    fetch, site lookup, client list all returned real data)."""
    cfg = get_app_config("omada")
    if not cfg or not cfg.get("base_url") or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise IntegrationError("not_configured", "Omada Controller is not configured", http_status=409)
    base_url = cfg["base_url"].rstrip("/")
    cached = _omada_token_cache.get(base_url)
    if cached and cached["expires_at"] > time.time() + 30:
        return cached["access_token"], cached["omadac_id"], base_url
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            info = await client.get(f"{base_url}/api/info")
            if info.status_code >= 400:
                raise IntegrationError("provider_unavailable", f"Omada Controller returned HTTP {info.status_code} for /api/info")
            omadac_id = info.json()["result"]["omadacId"]
            token_response = await client.post(
                f"{base_url}/openapi/authorize/token",
                params={"grant_type": "client_credentials"},
                json={"omadacId": omadac_id, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"]},
            )
        if token_response.status_code >= 400:
            raise IntegrationError("permission_denied", f"Omada auth returned HTTP {token_response.status_code}", details=_safe_response(token_response))
        payload = token_response.json()
        if payload.get("errorCode"):
            raise IntegrationError("permission_denied", payload.get("msg", "Omada authorization failed"), details=payload)
        result = payload["result"]
        access_token = result["accessToken"]
        expires_in = int(result.get("expiresIn", 7200))
        _omada_token_cache[base_url] = {"access_token": access_token, "omadac_id": omadac_id, "expires_at": time.time() + expires_in}
        return access_token, omadac_id, base_url
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


async def _omada_site_id(client: httpx.AsyncClient, base_url: str, omadac_id: str, headers: dict[str, str]) -> str:
    cfg = get_app_config("omada") or {}
    if cfg.get("site_id"):
        return cfg["site_id"]
    response = await client.get(f"{base_url}/openapi/v1/{omadac_id}/sites", headers=headers, params={"pageSize": 10, "page": 1})
    payload = response.json()
    sites = ((payload.get("result") or {}).get("data")) or []
    if not sites:
        raise IntegrationError("not_configured", "No sites found on this Omada Controller")
    site_id = sites[0]["siteId"]
    set_app_config("omada", {**cfg, "site_id": site_id}, cfg.get("configured_by", "system"))
    return site_id


async def omada_request(method: str, path: str, **kwargs) -> Any:
    """path is relative to /openapi/v1/{omadacId}/sites/{siteId}/ — pass e.g.
    "clients" or "clients/AA:BB:CC:DD:EE:FF/block". Read-only by convention
    from the caller's side (orchestrator gates writes behind confirmation);
    this function itself just executes whatever request it's given."""
    access_token, omadac_id, base_url = await _omada_token()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"AccessToken={access_token}"
    try:
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            site_id = await _omada_site_id(client, base_url, omadac_id, headers)
            url = f"{base_url}/openapi/v1/{omadac_id}/sites/{site_id}/{path.lstrip('/')}"
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise IntegrationError("permission_denied" if response.status_code in (401, 403) else "error", f"Omada Controller returned HTTP {response.status_code}", details=_safe_response(response))
        return response.json() if response.content else {"status": "ok"}
    except httpx.RequestError as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


def _ami_packet(fields: dict[str, Any]) -> bytes:
    safe_lines = []
    for key, value in fields.items():
        key_text = str(key)
        value_text = str(value)
        if "\r" in key_text or "\n" in key_text or "\r" in value_text or "\n" in value_text:
            raise IntegrationError("invalid_request", "Asterisk AMI fields cannot contain line breaks", http_status=400)
        safe_lines.append(f"{key_text}: {value_text}")
    return ("\r\n".join(safe_lines) + "\r\n\r\n").encode("utf-8")


def _ami_read(sock: socket.socket, timeout: float = 10.0) -> dict[str, str]:
    sock.settimeout(timeout)
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    text = data.decode("utf-8", errors="replace")
    result: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ": " in line:
            key, value = line.split(": ", 1)
            result[key] = value
    return result


def _ami_originate_sync(payload: dict[str, Any]) -> dict[str, str]:
    cfg = get_app_config("asterisk")
    if not cfg or not cfg.get("host") or not cfg.get("username") or not cfg.get("secret"):
        raise IntegrationError("not_configured", "Asterisk AMI is not configured", http_status=409)
    host = cfg["host"]
    port = int(cfg.get("port", 5038))
    use_tls = bool(cfg.get("tls", False))
    try:
        raw_sock = socket.create_connection((host, port), timeout=10)
        sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host) if use_tls else raw_sock
        with sock:
            _ami_read(sock, 3)
            sock.sendall(_ami_packet({"Action": "Login", "Username": cfg["username"], "Secret": cfg["secret"], "Events": "off"}))
            login = _ami_read(sock)
            if login.get("Response") != "Success":
                raise IntegrationError("permission_denied", "Asterisk AMI login failed", details=login)
            action_id = uuid.uuid4().hex
            fields = {
                "Action": "Originate", "ActionID": action_id, "Channel": payload["channel"], "Context": payload.get("context", cfg.get("context", "from-internal")),
                "Exten": payload["extension"], "Priority": payload.get("priority", 1), "Timeout": payload.get("timeout_ms", 30000), "Async": "true",
            }
            if payload.get("caller_id"):
                fields["CallerID"] = payload["caller_id"]
            sock.sendall(_ami_packet(fields))
            result = _ami_read(sock)
            sock.sendall(_ami_packet({"Action": "Logoff"}))
            if result.get("Response") != "Success":
                raise IntegrationError("error", "Asterisk rejected Originate", details=result)
            return result
    except IntegrationError:
        raise
    except (OSError, socket.timeout, ssl.SSLError) as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


def _ami_probe_sync() -> dict[str, str]:
    cfg = get_app_config("asterisk")
    if not cfg or not cfg.get("host") or not cfg.get("username") or not cfg.get("secret"):
        raise IntegrationError("not_configured", "Asterisk AMI is not configured", http_status=409)
    host = cfg["host"]
    port = int(cfg.get("port", 5038))
    use_tls = bool(cfg.get("tls", False))
    try:
        raw_sock = socket.create_connection((host, port), timeout=5)
        sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host) if use_tls else raw_sock
        with sock:
            _ami_read(sock, 3)
            sock.sendall(_ami_packet({"Action": "Login", "Username": cfg["username"], "Secret": cfg["secret"], "Events": "off"}))
            login = _ami_read(sock, 5)
            if login.get("Response") != "Success":
                raise IntegrationError("permission_denied", "Asterisk AMI login failed", details=login)
            sock.sendall(_ami_packet({"Action": "Logoff"}))
            return login
    except IntegrationError:
        raise
    except (OSError, socket.timeout, ssl.SSLError) as exc:
        raise IntegrationError("provider_unavailable", str(exc)) from exc


async def asterisk_originate(payload: dict[str, Any]) -> dict[str, str]:
    return await asyncio.to_thread(_ami_originate_sync, payload)


async def integration_probe(user_id: str, provider: str) -> dict[str, Any]:
    try:
        if provider == "homeassistant":
            data = await homeassistant_request("GET", "/api/")
            return {"provider": provider, "status": "ready", "detail": data}
        if provider == "emby":
            data = await emby_request("GET", "/System/Info/Public")
            return {"provider": provider, "status": "ready", "detail": {"ServerName": data.get("ServerName"), "Version": data.get("Version")}}
        if provider == "asterisk":
            cfg = get_app_config("asterisk")
            if not cfg:
                return {"provider": provider, "status": "not_configured"}
            await asyncio.to_thread(_ami_probe_sync)
            return {"provider": provider, "status": "ready"}
        if provider == "google_gmail":
            await gmail_list(user_id, max_results=1)
        elif provider == "google_calendar":
            await calendar_list(user_id, max_results=1)
        elif provider == "google_tasks":
            await tasks_list(user_id, max_results=1)
        elif provider == "google_youtube":
            await youtube_channel(user_id)
        elif provider == "spotify":
            await spotify_profile(user_id)
        elif provider == "tiktok":
            await tiktok_user(user_id)
        else:
            return {"provider": provider, "status": "unsupported_by_provider"}
        return {"provider": provider, "status": "ready"}
    except IntegrationError as exc:
        return {"provider": provider, "status": exc.status, "error": exc.message, "details": exc.details}
