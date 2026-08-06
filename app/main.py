from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import APP_NAME, APP_VERSION, COOKIE_SECURE, DISTROKID_UPLOAD_URL, MEDIA_DIR, RELEASE_DIR, SESSION_COOKIE, SESSION_TTL_SECONDS, TEMP_DIR, UI_DIR
from .cliproxy import bootstrap as bootstrap_cliproxy, model_catalog, secrets_data as cliproxy_secrets
from .db import connect, init_db, utcnow
from .integrations import (
    GOOGLE_SCOPE_PRESETS,
    IntegrationError,
    asterisk_originate,
    calendar_create,
    calendar_delete,
    calendar_list,
    calendar_update,
    connection_status,
    emby_request,
    get_app_config,
    get_connection,
    get_system_setting,
    gmail_get,
    gmail_list,
    gmail_send,
    google_authorization_url,
    google_callback,
    homeassistant_request,
    instagram_authorization_url,
    instagram_callback,
    instagram_media,
    instagram_profile,
    instagram_publish,
    integration_probe,
    revoke_connection,
    safe_media_path,
    set_app_config,
    set_system_setting,
    spotify_authorization_url,
    spotify_callback,
    spotify_create_playlist,
    spotify_current,
    spotify_devices,
    spotify_playback,
    spotify_playlists,
    spotify_save_tracks,
    spotify_saved_tracks,
    tasks_create,
    tasks_delete,
    tasks_list,
    tasks_update,
    tiktok_authorization_url,
    tiktok_callback,
    tiktok_creator_info,
    tiktok_publish,
    tiktok_publish_status,
    tiktok_user,
    tiktok_videos,
    youtube_analytics,
    youtube_channel,
    youtube_comments,
    youtube_reply_comment,
    youtube_upload,
)
from .memory import add_memory, delete_memory, search_memory
from .notify import add_channel as add_notify_channel, list_channels as list_notify_channels, remove_channel as remove_notify_channel, send_notification
from .orchestrator import ACTION_CAPABILITIES, ask
from .permissions import ALL_CAPABILITIES, is_allowed, permission_snapshot, set_permission
from .learning import run_daily_reflection
from .mcp_client import add_server as add_mcp_server, list_servers as list_mcp_servers, refresh_tool_cache, remove_server as remove_mcp_server, set_enabled as set_mcp_server_enabled
from .routines import create_routine, delete_routine as delete_routine_row, get_routine, list_routines, set_enabled as set_routine_enabled
from .scheduler import schedule_routine, start_scheduler, stop_scheduler, unschedule_routine
from .satellite import run_satellite_session
from .security import (
    audit,
    consume_confirmation,
    create_satellite_token,
    create_session,
    delete_session,
    get_session,
    issue_confirmation,
    list_satellite_tokens,
    password_hash,
    password_verify,
    resolve_satellite_token,
    revoke_satellite_token,
)
from .ui import esc, layout, status_card
from .voice import VoiceBackendError, elevenlabs_configured, elevenlabs_synthesize, elevenlabs_transcribe, enroll_voice, remove_voiceprint, runtime_status as voice_runtime_status, synthesize, transcribe, verify_voice

logger = logging.getLogger("athena.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bootstrap_cliproxy()
    cleanup_expired()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(self)"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:"
    return response


@app.exception_handler(IntegrationError)
async def integration_error_handler(_: Request, exc: IntegrationError):
    return JSONResponse(status_code=exc.http_status, content={"detail": {"status": exc.status, "message": exc.message, "details": exc.details}})


def cleanup_expired() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM oauth_states WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM confirmations WHERE expires_at<=? OR used_at IS NOT NULL", (now,))
        consents = conn.execute("SELECT user_id,retention_hours FROM location_consent").fetchall()
        for row in consents:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(row["retention_hours"]))).isoformat()
            conn.execute("DELETE FROM locations WHERE user_id=? AND created_at<?", (row["user_id"], cutoff))


def setup_required() -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None


def current_user(request: Request):
    user = get_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def page_user(request: Request):
    user = get_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return None
    return user


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator required")
    return user


def require_capability(user, capability: str) -> None:
    if not is_allowed(user, capability):
        raise HTTPException(403, f"Permission denied: {capability}")


def verify_csrf(request: Request, user, submitted: str | None = None) -> None:
    supplied = submitted or request.headers.get("X-CSRF-Token")
    if not supplied or not secrets.compare_digest(str(supplied), str(user["csrf_token"])):
        raise HTTPException(403, "Invalid CSRF token")


def require_confirmation(request: Request, user, action: str, payload: Any) -> None:
    token = request.headers.get("X-ATHENA-Confirmation")
    if not consume_confirmation(token, user["id"], user["session_hash"], action, payload):
        raise HTTPException(409, {"status": "confirmation_required", "action": action})


def request_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def normalized_username(value: str) -> str:
    username = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        raise HTTPException(400, "Username must be 3-64 characters using letters, numbers, dot, underscore or hyphen")
    return username


def normalized_display_name(value: str) -> str:
    display_name = value.strip()
    if not 1 <= len(display_name) <= 80 or any(ch in display_name for ch in "\r\n"):
        raise HTTPException(400, "Display name must be between 1 and 80 characters")
    return display_name


def set_login_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=SESSION_TTL_SECONDS, path="/")


async def service_statuses(user_id: str) -> list[dict[str, Any]]:
    result = [
        connection_status(user_id, "google_gmail", app_provider="google"),
        connection_status(user_id, "google_calendar", app_provider="google"),
        connection_status(user_id, "google_tasks", app_provider="google"),
        connection_status(user_id, "google_youtube", app_provider="google"),
        connection_status(user_id, "spotify"),
        connection_status(user_id, "tiktok"),
        connection_status(user_id, "instagram"),
    ]
    required_fields = {
        "homeassistant": ("base_url", "token"),
        "emby": ("base_url", "api_key"),
        "asterisk": ("host", "username", "secret"),
        "elevenlabs": ("api_key",),
    }
    for provider, fields in required_fields.items():
        cfg = get_app_config(provider) or {}
        result.append({"provider": provider, "status": "connected" if all(cfg.get(field) for field in fields) else "not_configured"})
    router = await model_catalog()
    result.append({
        "provider": "llm_router",
        "status": router.get("status", "error"),
        "selection": "athena_automatic",
        "connected_families": router.get("connected_families", []),
        "missing_families": router.get("missing_families", []),
        "detail": router.get("detail"),
        "error": router.get("error"),
    })
    with connect() as conn:
        voice = conn.execute("SELECT 1 FROM voiceprints WHERE user_id=?", (user_id,)).fetchone()
    runtime = voice_runtime_status()
    result.extend([
        {"provider": "voice_stt", "status": runtime["status"], "detail": runtime},
        {"provider": "voice_tts", "status": runtime["status"], "detail": runtime},
        {"provider": "voice_id", "status": "connected" if voice else "authorization_required", "runtime": runtime["status"]},
        {"provider": "distrokid", "status": "ready", "detail": "release_workspace_with_manual_final_submission"},
    ])
    return result


@app.get("/health")
async def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "setup_required" if setup_required() else "ok", "version": APP_VERSION, "database": "ready"}
    except sqlite3.Error as exc:
        return JSONResponse(status_code=503, content={"status": "error", "database": str(exc), "version": APP_VERSION})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if setup_required():
        return RedirectResponse("/setup", 303)
    user = page_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    statuses = await service_statuses(user["id"])
    cards = "".join(status_card(item) for item in statuses)
    body = f"""
    <div class='hero'><img src='/static/avatar.jpg' alt=''><div><h1>Γεια σου, {esc(user['display_name'])}</h1><p>{esc(user['role'])} · ATHENA είναι έτοιμη</p></div></div>
    <div class='card'><h2>Συνομιλία</h2><textarea id='q' placeholder='Γράψε τι χρειάζεσαι'></textarea><button onclick="askAthena()">Αποστολή</button><pre id='answer'></pre></div>
    <div class='grid'>{cards}</div>
    <script>async function askAthena(){{let q=document.getElementById('q').value;let out=document.getElementById('answer');out.textContent='...';try{{let d=await api('/api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q}})}});out.textContent=d.answer||pretty(d)}}catch(e){{out.textContent=e.message}}}}</script>
    """
    return layout("Αρχική", body, user, user["csrf_token"])


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if not setup_required():
        return RedirectResponse("/login", 303)
    body = """<img class='auth-mark' src='/static/avatar.jpg' alt='ATHENA'>
    <h1 style='text-align:center'>Πρώτη εγκατάσταση</h1>
    <div class='card'><form method='post' action='/setup'>
    <label>Όνομα εμφάνισης<input name='display_name' required maxlength='80'></label>
    <label>Όνομα χρήστη<input name='username' required minlength='3' maxlength='64' autocomplete='username'></label>
    <label>Κωδικός διαχειριστή<input name='password' type='password' required minlength='12' autocomplete='new-password'></label>
    <button style='width:100%'>Δημιουργία διαχειριστή</button></form><p class='muted' style='margin-top:12px'>Τα credentials των υπηρεσιών θα καταχωριστούν μετά το login. Δεν υπάρχουν ενσωματωμένα προσωπικά δεδομένα.</p></div>"""
    return layout("Πρώτη εγκατάσταση", body, centered=True)


@app.post("/setup")
async def setup_submit(display_name: str = Form(...), username: str = Form(...), password: str = Form(...)):
    if not setup_required():
        raise HTTPException(409, "Setup already completed")
    if len(password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    username = normalized_username(username)
    display_name = normalized_display_name(display_name)
    now = utcnow()
    user_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, username, display_name, password_hash(password), "admin", now, now))
    token, _ = create_session(user_id)
    audit(user_id, "initial_setup_completed", {})
    response = RedirectResponse("/graph", 303)
    set_login_cookie(response, token)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if setup_required():
        return RedirectResponse("/setup", 303)
    body = """<img class='auth-mark' src='/static/avatar.jpg' alt='ATHENA'>
    <h1 style='text-align:center'>Σύνδεση</h1>
    <div class='card'><form method='post' action='/login'>
    <label>Όνομα χρήστη<input name='username' required autocomplete='username'></label>
    <label>Κωδικός<input name='password' type='password' required autocomplete='current-password'></label>
    <button style='width:100%'>Σύνδεση</button></form></div>"""
    return layout("Σύνδεση", body, centered=True)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    normalized = username.strip()
    remote = client_ip(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM login_failures WHERE created_at<?", (cutoff,))
        failures = conn.execute("SELECT COUNT(*) AS c FROM login_failures WHERE username=? COLLATE NOCASE AND remote_addr=? AND created_at>=?", (normalized, remote, cutoff)).fetchone()["c"]
        if failures >= 8:
            raise HTTPException(429, "Too many failed login attempts. Try again later.")
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (normalized,)).fetchone()
    if not row or not password_verify(row["password_hash"], password):
        with connect() as conn:
            conn.execute("INSERT INTO login_failures(id,username,remote_addr,created_at) VALUES(?,?,?,?)", (str(uuid.uuid4()), normalized, remote, utcnow()))
        audit(row["id"] if row else None, "login_failed", {"username": normalized}, remote)
        raise HTTPException(401, "Invalid credentials")
    with connect() as conn:
        conn.execute("DELETE FROM login_failures WHERE username=? COLLATE NOCASE AND remote_addr=?", (normalized, remote))
    token, _ = create_session(row["id"])
    audit(row["id"], "login_success", {}, remote)
    response = RedirectResponse("/graph", 303)
    set_login_cookie(response, token)
    return response


@app.get("/logout")
async def logout(request: Request):
    user = page_user(request)
    delete_session(request.cookies.get(SESSION_COOKIE))
    if user:
        audit(user["id"], "logout", {}, client_ip(request))
    response = RedirectResponse("/login", 303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, user=Depends(require_admin)):
    with connect() as conn:
        users = conn.execute("SELECT id,username,display_name,role,active,created_at FROM users ORDER BY created_at").fetchall()
    rows = "".join(
        f"<tr><td>{esc(r['display_name'])}</td><td>{esc(r['username'])}</td><td>{esc(r['role'])}</td>"
        f"<td>{'ναι' if r['active'] else 'όχι'}</td><td><a class='button secondary' href='/admin/users/{r['id']}/permissions'>Δικαιώματα</a> "
        f"<a class='button secondary' href='/admin/users/{r['id']}/edit'>Επεξεργασία</a></td></tr>" for r in users
    )
    body = f"""<h1>Χρήστες οικογένειας</h1><div class='card'><table><tr><th>Όνομα</th><th>Username</th><th>Ρόλος</th><th>Ενεργός</th><th></th></tr>{rows}</table></div>
    <div class='card'><h2>Νέος χρήστης</h2><form method='post' action='/admin/users'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Όνομα<input name='display_name' required></label><label>Username<input name='username' required></label><label>Ρόλος<select name='role'><option>adult</option><option>child</option><option>guest</option><option>admin</option></select></label><label>Προσωρινός κωδικός<input name='password' type='password' required minlength='12'></label><button>Δημιουργία</button></form></div>"""
    return layout("Χρήστες", body, user, user["csrf_token"])


@app.post("/admin/users")
async def admin_create_user(request: Request, display_name: str = Form(...), username: str = Form(...), role: str = Form(...), password: str = Form(...), csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    if role not in {"admin", "adult", "child", "guest"} or len(password) < 12:
        raise HTTPException(400, "Invalid role or password")
    username = normalized_username(username)
    display_name = normalized_display_name(display_name)
    target_id = str(uuid.uuid4())
    now = utcnow()
    try:
        with connect() as conn:
            conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (target_id, username, display_name, password_hash(password), role, now, now))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Username already exists") from exc
    audit(user["id"], "user_created", {"target_user": target_id, "role": role}, client_ip(request))
    return RedirectResponse("/admin/users", 303)


@app.get("/admin/users/{target_id}/edit", response_class=HTMLResponse)
async def admin_user_edit_page(target_id: str, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        target = conn.execute("SELECT id,username,display_name,role,active FROM users WHERE id=?", (target_id,)).fetchone()
        active_admins = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1").fetchone()["c"]
    if not target:
        raise HTTPException(404, "User not found")
    roles = "".join(f"<option value='{role}' {'selected' if target['role']==role else ''}>{role}</option>" for role in ("admin", "adult", "child", "guest"))
    body = f"""<h1>Επεξεργασία χρήστη</h1><div class='card'><form method='post'>
    <input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Όνομα εμφάνισης<input name='display_name' value='{esc(target['display_name'])}' required></label>
    <label>Username<input name='username' value='{esc(target['username'])}' required></label><label>Ρόλος<select name='role'>{roles}</select></label>
    <label class='inline'><input type='checkbox' name='active' value='true' {'checked' if target['active'] else ''}>Ενεργός λογαριασμός</label>
    <button>Αποθήκευση</button></form></div><div class='card'><h2>Αλλαγή κωδικού</h2><form method='post' action='/admin/users/{esc(target_id)}/password'>
    <input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Νέος κωδικός<input name='password' type='password' minlength='12' required></label><button>Αλλαγή κωδικού</button></form></div>
    <p class='muted'>Ενεργοί διαχειριστές: {active_admins}. Η ATHENA δεν επιτρέπει απενεργοποίηση του τελευταίου ενεργού administrator.</p>"""
    return layout("Επεξεργασία χρήστη", body, user, user["csrf_token"])


@app.post("/admin/users/{target_id}/edit")
async def admin_user_edit(target_id: str, request: Request, display_name: str = Form(...), username: str = Form(...), role: str = Form(...), active: bool = Form(False), csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    if role not in {"admin", "adult", "child", "guest"}:
        raise HTTPException(400, "Invalid role")
    username = normalized_username(username)
    display_name = normalized_display_name(display_name)
    with connect() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if target["role"] == "admin" and target["active"] and (role != "admin" or not active):
            count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1", ()).fetchone()["c"]
            if count <= 1:
                raise HTTPException(409, "Cannot disable or demote the last active administrator")
        try:
            conn.execute("UPDATE users SET username=?,display_name=?,role=?,active=?,updated_at=? WHERE id=?", (username, display_name, role, int(active), utcnow(), target_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Username already exists") from exc
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
    audit(user["id"], "user_updated", {"target_user": target_id, "role": role, "active": active}, client_ip(request))
    return RedirectResponse("/admin/users", 303)


@app.post("/admin/users/{target_id}/password")
async def admin_user_password(target_id: str, request: Request, password: str = Form(...), csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    if len(password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (target_id,)).fetchone():
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (password_hash(password), utcnow(), target_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
    audit(user["id"], "user_password_reset", {"target_user": target_id}, client_ip(request))
    return RedirectResponse(f"/admin/users/{target_id}/edit", 303)


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user=Depends(current_user)):
    body = f"""<h1>Λογαριασμός</h1><div class='card'><p><strong>{esc(user['display_name'])}</strong> · {esc(user['username'])} · {esc(user['role'])}</p>
    <form method='post' action='/account/password'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Τρέχων κωδικός<input name='current_password' type='password' required></label>
    <label>Νέος κωδικός<input name='new_password' type='password' minlength='12' required></label><button>Αλλαγή κωδικού και αποσύνδεση άλλων sessions</button></form></div>"""
    return layout("Λογαριασμός", body, user, user["csrf_token"])


@app.post("/account/password")
async def account_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), csrf: str = Form(...), user=Depends(current_user)):
    verify_csrf(request, user, csrf)
    if len(new_password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not stored or not password_verify(stored["password_hash"], current_password):
            raise HTTPException(401, "Current password is incorrect")
        conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (password_hash(new_password), utcnow(), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], user["session_hash"]))
    audit(user["id"], "password_changed", {}, client_ip(request))
    return RedirectResponse("/account", 303)


@app.get("/admin/users/{target_id}/permissions", response_class=HTMLResponse)
async def admin_permissions(target_id: str, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    snapshot = permission_snapshot(target_id, target["role"])
    checks = "".join(f"<label class='inline'><input type='checkbox' name='capability' value='{esc(cap)}' {'checked' if allowed else ''}>{esc(cap)}</label>" for cap, allowed in snapshot.items())
    body = f"<h1>Δικαιώματα: {esc(target['display_name'])}</h1><div class='card'><form method='post'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'>{checks}<button>Αποθήκευση</button></form></div>"
    return layout("Δικαιώματα", body, user, user["csrf_token"])


@app.post("/admin/users/{target_id}/permissions")
async def admin_permissions_save(target_id: str, request: Request, capability: list[str] = Form(default=[]), csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    selected = set(capability)
    for cap in ALL_CAPABILITIES:
        set_permission(target_id, cap, cap in selected)
    audit(user["id"], "permissions_updated", {"target_user": target_id, "enabled": sorted(selected)}, client_ip(request))
    return RedirectResponse(f"/admin/users/{target_id}/permissions", 303)


@app.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request, user=Depends(current_user)):
    statuses = await service_statuses(user["id"])
    cards = "".join(status_card(s) for s in statuses)
    google_buttons = "".join(
        f"<div><strong>Google {esc(service)}</strong> "
        f"<a class='button secondary' href='/oauth/google/{service}/start?mode=read'>Σύνδεση μόνο ανάγνωσης</a> "
        f"<a class='button' href='/oauth/google/{service}/start?mode=write'>Σύνδεση ανάγνωσης/εγγραφής</a></div>"
        for service in GOOGLE_SCOPE_PRESETS
    )
    connection_rows = []
    for status in statuses:
        provider = status["provider"]
        if provider not in {"google_gmail", "google_calendar", "google_tasks", "google_youtube", "spotify", "tiktok", "instagram"}:
            continue
        scopes = esc(status.get("scopes", ""))
        actions = ""
        if status["status"] in {"connected", "ready", "token_expired", "permission_denied", "error"}:
            actions = (
                f"<form method='post' action='/integrations/{esc(provider)}/revoke' style='display:inline'>"
                f"<input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'>"
                f"<label>Κωδικός<input name='password' type='password' required></label>"
                f"<button class='danger'>Ανάκληση και διαγραφή tokens</button></form>"
            )
        connection_rows.append(
            f"<tr><td>{esc(provider)}</td><td>{esc(status['status'])}</td><td>{esc(status.get('account',''))}</td><td><small>{scopes}</small></td><td>{actions}</td></tr>"
        )
    personal = (
        f"<div class='card'><h2>Προσωπικές συνδέσεις</h2><p>Κάθε μέλος συνδέει αποκλειστικά τον δικό του λογαριασμό και επιλέγει ανεξάρτητα scopes. Η ανάκληση οποιασδήποτε Google σύνδεσης ανακαλεί το συνολικό OAuth grant της εφαρμογής και διαγράφει όλες τις τοπικές Google συνδέσεις του ίδιου χρήστη.</p>"
        f"{google_buttons}<p><a class='button' href='/oauth/spotify/start'>Σύνδεση Spotify</a> "
        f"<a class='button' href='/oauth/tiktok/start'>Σύνδεση TikTok</a> "
        f"<a class='button' href='/oauth/instagram/start'>Σύνδεση Instagram</a> "
        f"<small>(μόνο Professional/Creator λογαριασμός — δεν υπάρχει API για personal)</small></p></div>"
        f"<div class='card'><table><tr><th>Connector</th><th>Status</th><th>Account</th><th>Scopes</th><th></th></tr>{''.join(connection_rows)}</table></div>"
    )
    admin = ""
    if user["role"] == "admin":
        google = get_app_config("google") or {}
        spotify = get_app_config("spotify") or {}
        tiktok = get_app_config("tiktok") or {}
        instagram = get_app_config("instagram") or {}
        elevenlabs = get_app_config("elevenlabs") or {}
        ha = get_app_config("homeassistant") or {}
        emby = get_app_config("emby") or {}
        asterisk = get_app_config("asterisk") or {}
        voice_cfg = get_system_setting("voice", {}) or {}
        public = get_system_setting("public_base_url", "") or ""
        secret_note = "<small>Άφησε κενό ένα secret για να διατηρηθεί η ήδη αποθηκευμένη τιμή.</small>"
        admin = f"""<div class='card'><h2>Ρύθμιση εφαρμογών και συστημάτων</h2><form method='post' action='/integrations/config'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'>
        <h3>Public URL</h3><label>Εξωτερικό HTTPS URL για OAuth callbacks<input name='public_base_url' value='{esc(public)}' placeholder='https://athena.example.com'></label>
        <h3>Google OAuth Web App</h3><label>Client ID<input name='google_client_id' value='{esc(google.get('client_id',''))}'></label><label>Client secret<input name='google_client_secret' type='password'></label>{secret_note}
        <h3>Spotify App — PKCE</h3><label>Client ID<input name='spotify_client_id' value='{esc(spotify.get('client_id',''))}'></label><label>Client secret (προαιρετικό, δεν απαιτείται από PKCE)<input name='spotify_client_secret' type='password'></label>
        <h3>TikTok App</h3><label>Client key<input name='tiktok_client_key' value='{esc(tiktok.get('client_key',''))}'></label><label>Client secret<input name='tiktok_client_secret' type='password'></label>{secret_note}
        <h3>Instagram App — "Instagram API with Instagram Login"</h3><p><small>Ο Instagram λογαριασμός πρέπει να είναι Professional (Business ή Creator) — δεν υπάρχει API για personal accounts.</small></p><label>App ID<input name='instagram_client_id' value='{esc(instagram.get('client_id',''))}'></label><label>App secret<input name='instagram_client_secret' type='password'></label>{secret_note}
        <h3>LLM Brain — router-for-me/CLIProxyAPI</h3><p>Η ATHENA χρησιμοποιεί αποκλειστικά το ενσωματωμένο CLIProxyAPI. Η σύνδεση Claude, Codex/OpenAI, Gemini και Grok γίνεται από το <a class='button secondary' href='/admin/llm'>LLM Router</a>.</p>
        <h3>Home Assistant</h3><label>Base URL<input name='ha_base_url' value='{esc(ha.get('base_url',''))}' placeholder='http://192.168.1.10:8123'></label><label>Long-lived token<input name='ha_token' type='password'></label><label>Allowed domains, comma-separated<input name='ha_allowed_domains' value='{esc(','.join(ha.get('allowed_domains', ['light','switch','media_player','cover','lock','climate','scene','script'])))}'></label>{secret_note}
        <h3>Emby</h3><label>Base URL<input name='emby_base_url' value='{esc(emby.get('base_url',''))}'></label><label>API key<input name='emby_api_key' type='password'></label><label>User ID<input name='emby_user_id' value='{esc(emby.get('user_id',''))}'></label>{secret_note}
        <h3>Asterisk AMI</h3><label>Host<input name='asterisk_host' value='{esc(asterisk.get('host',''))}'></label><label>Port<input name='asterisk_port' value='{esc(asterisk.get('port',5038))}'></label><label>Username<input name='asterisk_username' value='{esc(asterisk.get('username',''))}'></label><label>Secret<input name='asterisk_secret' type='password'></label><label>Context<input name='asterisk_context' value='{esc(asterisk.get('context','from-internal'))}'></label><label class='inline'><input type='checkbox' name='asterisk_tls' value='true' {'checked' if asterisk.get('tls') else ''}>TLS</label>{secret_note}
        <h3>ElevenLabs (voice — preferred over local Whisper/Piper when set)</h3><p><small>Free tier is enough to try it. Χρησιμοποιείται αυτόματα για STT/TTS όποτε υπάρχει API key εδώ· χωρίς αυτό πέφτει στο τοπικό Whisper/Piper. Για ελληνική φωνή, βρες ελληνικό voice στο <a href='https://elevenlabs.io/app/voice-library' target='_blank' rel='noopener'>Voice Library</a> του ElevenLabs και βάλε το Voice ID του εδώ.</small></p><label>API key<input name='elevenlabs_api_key' type='password'></label><label>Default voice ID (ελληνικό, από το Voice Library)<input name='elevenlabs_default_voice_id' value='{esc(elevenlabs.get("default_voice_id",""))}' placeholder='π.χ. π78ab...'></label><label>Model ID<input name='elevenlabs_model_id' value='{esc(elevenlabs.get("model_id","eleven_multilingual_v2"))}'></label><small>Δοκίμασε <code>eleven_v3</code> αν το πλάνο σου το υποστηρίζει — πιο φυσική προφορά.</small>{secret_note}
        <h3>Local voice runtime</h3><label>Whisper model<input name='voice_stt_model' value='{esc(voice_cfg.get('stt_model','small'))}'></label><label>Device<input name='voice_stt_device' value='{esc(voice_cfg.get('stt_device','cpu'))}'></label><label>Compute type<input name='voice_stt_compute_type' value='{esc(voice_cfg.get('stt_compute_type','int8'))}'></label><label>Piper voice<input name='voice_tts_voice' value='{esc(voice_cfg.get('tts_voice','el_GR-rapunzelina-low'))}'></label><label>Wake phrase<input name='voice_wake_phrase' value='{esc(voice_cfg.get('wake_phrase','Αθηνά'))}'></label>
        <button>Αποθήκευση ρυθμίσεων</button></form></div>"""
    body = f"<h1>Συνδέσεις</h1><div class='grid'>{cards}</div>{personal}{admin}"
    return layout("Συνδέσεις", body, user, user["csrf_token"])


@app.post("/integrations/config")
async def integrations_config(request: Request, csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    form = await request.form()
    allowed_domains = [item.strip() for item in str(form.get("ha_allowed_domains") or "").split(",") if item.strip()]
    mapping = {
        "google": {"client_id": form.get("google_client_id"), "client_secret": form.get("google_client_secret")},
        "spotify": {"client_id": form.get("spotify_client_id"), "client_secret": form.get("spotify_client_secret")},
        "tiktok": {"client_key": form.get("tiktok_client_key"), "client_secret": form.get("tiktok_client_secret")},
        "instagram": {"client_id": form.get("instagram_client_id"), "client_secret": form.get("instagram_client_secret")},
        "elevenlabs": {"api_key": form.get("elevenlabs_api_key"), "default_voice_id": form.get("elevenlabs_default_voice_id"), "model_id": form.get("elevenlabs_model_id") or "eleven_multilingual_v2"},
        "homeassistant": {"base_url": form.get("ha_base_url"), "token": form.get("ha_token"), "allowed_domains": allowed_domains or None},
        "emby": {"base_url": form.get("emby_base_url"), "api_key": form.get("emby_api_key"), "user_id": form.get("emby_user_id")},
        "asterisk": {"host": form.get("asterisk_host"), "port": int(form.get("asterisk_port") or 5038), "username": form.get("asterisk_username"), "secret": form.get("asterisk_secret"), "context": form.get("asterisk_context") or "from-internal", "tls": form.get("asterisk_tls") == "true"},
    }
    for provider, values in mapping.items():
        previous = get_app_config(provider) or {}
        merged = dict(previous)
        for key, value in values.items():
            if value not in (None, "", []):
                merged[key] = value
            elif key in {"tls"}:
                merged[key] = value
        if merged and any(v not in (None, "", []) for v in merged.values()):
            set_app_config(provider, merged, user["id"])
    public = str(form.get("public_base_url") or "").strip().rstrip("/")
    if public:
        if not public.startswith(("https://", "http://")):
            raise HTTPException(400, "Public base URL must start with http:// or https://")
        set_system_setting("public_base_url", public, user["id"])
    voice_cfg = {
        "stt_model": str(form.get("voice_stt_model") or "small").strip(),
        "stt_device": str(form.get("voice_stt_device") or "cpu").strip(),
        "stt_compute_type": str(form.get("voice_stt_compute_type") or "int8").strip(),
        "tts_voice": str(form.get("voice_tts_voice") or "el_GR-rapunzelina-low").strip(),
        "wake_phrase": str(form.get("voice_wake_phrase") or "Αθηνά").strip(),
        "wake_model": "tiny",
        "wake_language": "el",
    }
    set_system_setting("voice", voice_cfg, user["id"])
    audit(user["id"], "integration_configuration_updated", {"providers": list(mapping)}, client_ip(request))
    return RedirectResponse("/integrations", 303)


@app.get("/admin/llm", response_class=HTMLResponse)
async def admin_llm_page(request: Request, user=Depends(require_admin)):
    catalog = await model_catalog()
    host = request.url.hostname or "localhost"
    management_url = f"http://{host}:8317/management.html"
    labels = {"claude": "Claude", "codex_openai": "Codex/OpenAI", "gemini": "Gemini", "grok": "Grok"}
    family_cards = []
    families = catalog.get("families", {})
    for family in ("claude", "codex_openai", "gemini", "grok"):
        models = families.get(family, [])
        state = "connected" if models else "authorization_required"
        model_list = "".join(f"<li><code>{esc(model)}</code></li>" for model in models) or "<li>Δεν έχει συνδεθεί λογαριασμός αυτής της οικογένειας.</li>"
        family_cards.append(f"<div class='card'><h3>{labels[family]}</h3><span class='status {state}'>{state}</span><ul>{model_list}</ul></div>")
    body = f"""<h1>ATHENA LLM Brain</h1>
    <div class='card' style='text-align:center;padding:0;overflow:hidden'>
      <img src='/static/brain.jpg' alt='' style='width:100%;max-height:340px;object-fit:cover;display:block'>
    </div>
    <div class='card'><h3>router-for-me/CLIProxyAPI</h3>
    <p>Status: <span class='status {esc(catalog.get('status','error'))}'>{esc(catalog.get('status','error'))}</span></p>
    <p>Οι Claude, Codex/OpenAI, Gemini και Grok αποτελούν τον εσωτερικό εγκέφαλο της ATHENA. Η ATHENA επιλέγει αυτόματα ποιο τμήμα του εγκεφάλου θα χρησιμοποιήσει ανά εργασία και εφαρμόζει αυτόματο fallback όταν ένα τμήμα δεν είναι διαθέσιμο.</p>
    <p><strong>Δεν υπάρχει και δεν επιτρέπεται χειροκίνητη επιλογή μοντέλου από χρήστη ή διαχειριστή.</strong></p>
    <p><a class='button' href='{esc(management_url)}' target='_blank' rel='noopener'>Άνοιγμα CLIProxyAPI Management</a></p></div>
    <div class='grid'>{''.join(family_cards)}</div>
    <div class='card'><h3>Management key</h3><p>Για εμφάνιση του κλειδιού διαχείρισης απαιτείται ξανά ο κωδικός διαχειριστή.</p>
    <form method='post' action='/admin/llm/credentials'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Κωδικός διαχειριστή<input name='password' type='password' required autocomplete='current-password'></label><button>Εμφάνιση κλειδιού</button></form></div>"""
    return layout("ATHENA LLM Brain", body, user, user["csrf_token"])


@app.post("/admin/llm/credentials", response_class=HTMLResponse)
async def admin_llm_credentials(request: Request, password: str = Form(...), csrf: str = Form(...), user=Depends(require_admin)):
    verify_csrf(request, user, csrf)
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not stored or not password_verify(stored["password_hash"], password):
        audit(user["id"], "cliproxy_management_key_denied", {}, client_ip(request))
        raise HTTPException(401, "Password confirmation failed")
    data = cliproxy_secrets()
    if not data:
        raise HTTPException(503, "CLIProxyAPI secrets are not initialized")
    audit(user["id"], "cliproxy_management_key_viewed", {}, client_ip(request))
    body = f"""<h1>CLIProxyAPI credentials</h1><div class='card'><p>Management key:</p><pre>{esc(data['management_key'])}</pre><p class='muted'>Το κλειδί αυτό χρησιμοποιείται μόνο στο CLIProxyAPI Management panel. Δεν είναι προσωπικό provider credential.</p><p><a class='button' href='/admin/llm'>Επιστροφή</a></p></div>"""
    return layout("CLIProxyAPI credentials", body, user, user["csrf_token"])


@app.get("/api/llm/models")
async def api_llm_models(user=Depends(current_user)):
    require_capability(user, "llm.use")
    return await model_catalog()


@app.post("/integrations/{provider}/revoke")
async def integration_revoke(provider: str, request: Request, password: str = Form(...), csrf: str = Form(...), user=Depends(current_user)):
    verify_csrf(request, user, csrf)
    if provider not in {"google_gmail", "google_calendar", "google_tasks", "google_youtube", "spotify", "tiktok", "instagram"}:
        raise HTTPException(400, "Unsupported personal connector")
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not stored or not password_verify(stored["password_hash"], password):
        raise HTTPException(401, "Password confirmation failed")
    result = await revoke_connection(user["id"], provider)
    audit(user["id"], "integration_revoked", result, client_ip(request))
    return RedirectResponse("/integrations", 303)


@app.get("/oauth/google/{service}/start")
async def oauth_google_start(service: str, request: Request, mode: str = "read", user=Depends(current_user)):
    require_capability(user, "integrations.connect")
    capability_map = {
        ("gmail", "read"): "gmail.read", ("gmail", "write"): "gmail.send",
        ("calendar", "read"): "calendar.read", ("calendar", "write"): "calendar.write",
        ("tasks", "read"): "tasks.read", ("tasks", "write"): "tasks.write",
        ("youtube", "read"): "youtube.read", ("youtube", "write"): "youtube.publish",
    }
    capability = capability_map.get((service, mode))
    if not capability:
        raise HTTPException(400, "Unsupported Google connector or mode")
    require_capability(user, capability)
    url = await google_authorization_url(user["id"], service, mode, request_base(request))
    return RedirectResponse(url, 302)


@app.get("/oauth/google/callback")
async def oauth_google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, f"Google authorization failed: {error or 'missing response'}")
    user_id, provider = await google_callback(code, state)
    audit(user_id, "oauth_connected", {"provider": provider}, client_ip(request))
    return RedirectResponse("/integrations", 303)


@app.get("/oauth/spotify/start")
async def oauth_spotify_start(request: Request, user=Depends(current_user)):
    require_capability(user, "integrations.connect")
    if not (is_allowed(user, "spotify.read") or is_allowed(user, "spotify.control")):
        raise HTTPException(403, "Spotify permissions are disabled for this user")
    return RedirectResponse(await spotify_authorization_url(user["id"], request_base(request)), 302)


@app.get("/oauth/spotify/callback")
async def oauth_spotify_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, f"Spotify authorization failed: {error or 'missing response'}")
    user_id, provider = await spotify_callback(code, state)
    audit(user_id, "oauth_connected", {"provider": provider}, client_ip(request))
    return RedirectResponse("/integrations", 303)


@app.get("/oauth/tiktok/start")
async def oauth_tiktok_start(request: Request, user=Depends(current_user)):
    require_capability(user, "integrations.connect")
    if not (is_allowed(user, "tiktok.read") or is_allowed(user, "tiktok.publish")):
        raise HTTPException(403, "TikTok permissions are disabled for this user")
    return RedirectResponse(await tiktok_authorization_url(user["id"], request_base(request)), 302)


@app.get("/oauth/tiktok/callback")
async def oauth_tiktok_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, f"TikTok authorization failed: {error_description or error or 'missing response'}")
    user_id, provider = await tiktok_callback(code, state)
    audit(user_id, "oauth_connected", {"provider": provider}, client_ip(request))
    return RedirectResponse("/integrations", 303)


@app.get("/oauth/instagram/start")
async def oauth_instagram_start(request: Request, user=Depends(current_user)):
    require_capability(user, "integrations.connect")
    if not (is_allowed(user, "instagram.read") or is_allowed(user, "instagram.publish")):
        raise HTTPException(403, "Instagram permissions are disabled for this user")
    return RedirectResponse(await instagram_authorization_url(user["id"], request_base(request)), 302)


@app.get("/oauth/instagram/callback")
async def oauth_instagram_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None, error_reason: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, f"Instagram authorization failed: {error_reason or error or 'missing response'}")
    user_id, provider = await instagram_callback(code, state)
    audit(user_id, "oauth_connected", {"provider": provider}, client_ip(request))
    return RedirectResponse("/integrations", 303)


class ConfirmationRequest(BaseModel):
    action: str = Field(min_length=3, max_length=120)
    payload: dict[str, Any]
    password: str = Field(min_length=1, max_length=512)


@app.post("/api/confirmations")
async def create_confirmation(payload: ConfirmationRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not stored or not password_verify(stored["password_hash"], payload.password):
        audit(user["id"], "confirmation_failed", {"action": payload.action}, client_ip(request))
        raise HTTPException(401, "Password confirmation failed")
    token = issue_confirmation(user["id"], user["session_hash"], payload.action, payload.payload)
    audit(user["id"], "confirmation_issued", {"action": payload.action}, client_ip(request))
    return {"status": "confirmation_issued", "token": token, "expires_in": 180}


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=12000)


@app.post("/api/ask")
async def api_ask(payload: AskRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "llm.use")
    result = await ask(user, payload.question)
    audit(user["id"], "assistant_question", {"status": result.get("status")}, client_ip(request))
    return result


@app.get("/api/status")
async def api_status(user=Depends(current_user)):
    return {"version": APP_VERSION, "user": {"id": user["id"], "display_name": user["display_name"], "role": user["role"]}, "services": await service_statuses(user["id"]), "permissions": permission_snapshot(user["id"], user["role"])}


@app.post("/api/readiness")
async def api_readiness(request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    providers = ["google_gmail", "google_calendar", "google_tasks", "google_youtube", "spotify", "tiktok", "homeassistant", "emby", "asterisk"]
    results = list(await asyncio.gather(*(integration_probe(user["id"], provider) for provider in providers)))
    router = await model_catalog()
    results.append({"provider": "llm_router", **router})
    statuses = {r["status"] for r in results}
    if statuses <= {"ready", "connected"}:
        overall = "ready"
    elif statuses & {"error", "degraded", "model_unavailable", "permission_denied", "provider_unavailable", "token_expired", "requires_provider_approval"}:
        overall = "degraded"
    elif "not_configured" in statuses:
        overall = "configuration_required"
    else:
        overall = "authorization_required"
    return {"status": overall, "results": results}


@app.get("/api/gmail/messages")
async def api_gmail_messages(q: str = "", max_results: int = 20, user=Depends(current_user)):
    require_capability(user, "gmail.read")
    return await gmail_list(user["id"], q, max_results)


@app.get("/api/gmail/messages/{message_id}")
async def api_gmail_message(message_id: str, format_name: Literal["full", "metadata", "minimal", "raw"] = "full", user=Depends(current_user)):
    require_capability(user, "gmail.read")
    return await gmail_get(user["id"], message_id, format_name)


class GmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""
    bcc: str = ""


@app.post("/api/gmail/send")
async def api_gmail_send(payload: GmailSendRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "gmail.send")
    data = payload.model_dump()
    require_confirmation(request, user, "gmail.send", data)
    result = await gmail_send(user["id"], **data)
    audit(user["id"], "gmail_sent", {"to": payload.to, "subject": payload.subject, "message_id": result.get("id")}, client_ip(request))
    return result


@app.get("/api/calendar/events")
async def api_calendar_events(time_min: str | None = None, max_results: int = 20, user=Depends(current_user)):
    require_capability(user, "calendar.read")
    return await calendar_list(user["id"], time_min, max_results)


class CalendarEventRequest(BaseModel):
    summary: str
    description: str = ""
    location: str = ""
    start: dict[str, Any]
    end: dict[str, Any]
    attendees: list[dict[str, str]] = Field(default_factory=list)


@app.post("/api/calendar/events")
async def api_calendar_create(payload: CalendarEventRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "calendar.write")
    data = payload.model_dump(exclude_none=True)
    require_confirmation(request, user, "calendar.create", data)
    result = await calendar_create(user["id"], data)
    audit(user["id"], "calendar_event_created", {"event_id": result.get("id"), "summary": payload.summary}, client_ip(request))
    return result


@app.patch("/api/calendar/events/{event_id}")
async def api_calendar_update(event_id: str, payload: CalendarEventRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "calendar.write")
    data = {"event_id": event_id, "event": payload.model_dump(exclude_none=True)}
    require_confirmation(request, user, "calendar.update", data)
    result = await calendar_update(user["id"], event_id, data["event"])
    audit(user["id"], "calendar_event_updated", {"event_id": event_id, "summary": payload.summary}, client_ip(request))
    return result


@app.delete("/api/calendar/events/{event_id}")
async def api_calendar_delete(event_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "calendar.write")
    data = {"event_id": event_id}
    require_confirmation(request, user, "calendar.delete", data)
    result = await calendar_delete(user["id"], event_id)
    audit(user["id"], "calendar_event_deleted", data, client_ip(request))
    return result


@app.get("/api/google-tasks")
async def api_google_tasks(max_results: int = 50, user=Depends(current_user)):
    require_capability(user, "tasks.read")
    return await tasks_list(user["id"], max_results=max_results)


class GoogleTaskRequest(BaseModel):
    title: str
    notes: str = ""
    due: str | None = None


@app.post("/api/google-tasks")
async def api_google_task_create(payload: GoogleTaskRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "tasks.write")
    data = payload.model_dump()
    require_confirmation(request, user, "google_tasks.create", data)
    result = await tasks_create(user["id"], payload.title, payload.notes, payload.due)
    audit(user["id"], "google_task_created", {"task_id": result.get("id"), "title": payload.title}, client_ip(request))
    return result


@app.patch("/api/google-tasks/{task_id}")
async def api_google_task_update(task_id: str, payload: GoogleTaskRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "tasks.write")
    task = payload.model_dump(exclude_none=True)
    data = {"task_id": task_id, "task": task}
    require_confirmation(request, user, "google_tasks.update", data)
    result = await tasks_update(user["id"], task_id, task)
    audit(user["id"], "google_task_updated", {"task_id": task_id, "title": payload.title}, client_ip(request))
    return result


@app.delete("/api/google-tasks/{task_id}")
async def api_google_task_delete(task_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "tasks.write")
    data = {"task_id": task_id}
    require_confirmation(request, user, "google_tasks.delete", data)
    result = await tasks_delete(user["id"], task_id)
    audit(user["id"], "google_task_deleted", data, client_ip(request))
    return result


@app.get("/api/youtube/channel")
async def api_youtube_channel(user=Depends(current_user)):
    require_capability(user, "youtube.read")
    return await youtube_channel(user["id"])


@app.get("/api/youtube/comments/{video_id}")
async def api_youtube_comments(video_id: str, max_results: int = 50, user=Depends(current_user)):
    require_capability(user, "youtube.read")
    return await youtube_comments(user["id"], video_id, max_results)


@app.get("/api/youtube/analytics")
async def api_youtube_analytics(start_date: str, end_date: str, metrics: str = "views,estimatedMinutesWatched,subscribersGained", dimensions: str = "day", user=Depends(current_user)):
    require_capability(user, "youtube.read")
    return await youtube_analytics(user["id"], start_date, end_date, metrics, dimensions)


class YouTubeCommentReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


@app.post("/api/youtube/comments/{parent_id}/reply")
async def api_youtube_comment_reply(parent_id: str, payload: YouTubeCommentReplyRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "youtube.publish")
    data = {"parent_id": parent_id, "text": payload.text}
    require_confirmation(request, user, "youtube.comment.reply", data)
    result = await youtube_reply_comment(user["id"], parent_id, payload.text)
    audit(user["id"], "youtube_comment_replied", {"parent_id": parent_id, "comment_id": result.get("id")}, client_ip(request))
    return result


class YouTubeUploadRequest(BaseModel):
    media_path: str
    title: str
    description: str = ""
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "22"
    tags: list[str] = Field(default_factory=list)


@app.post("/api/youtube/upload")
async def api_youtube_upload(payload: YouTubeUploadRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "youtube.publish")
    data = payload.model_dump()
    require_confirmation(request, user, "youtube.upload", data)
    result = await youtube_upload(user["id"], **data)
    audit(user["id"], "youtube_uploaded", {"video_id": result.get("id"), "title": payload.title}, client_ip(request))
    return result


@app.get("/api/spotify/playlists")
async def api_spotify_playlists(limit: int = 50, user=Depends(current_user)):
    require_capability(user, "spotify.read")
    return await spotify_playlists(user["id"], limit)


@app.get("/api/spotify/current")
async def api_spotify_current(user=Depends(current_user)):
    require_capability(user, "spotify.read")
    return await spotify_current(user["id"])


@app.get("/api/spotify/devices")
async def api_spotify_devices(user=Depends(current_user)):
    require_capability(user, "spotify.read")
    return await spotify_devices(user["id"])


@app.get("/api/spotify/saved-tracks")
async def api_spotify_saved_tracks(limit: int = 50, offset: int = 0, user=Depends(current_user)):
    require_capability(user, "spotify.read")
    return await spotify_saved_tracks(user["id"], limit, offset)


class SpotifySavedTracksRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)


@app.put("/api/spotify/saved-tracks")
async def api_spotify_save_tracks(payload: SpotifySavedTracksRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "spotify.control")
    data = payload.model_dump()
    require_confirmation(request, user, "spotify.saved_tracks.add", data)
    result = await spotify_save_tracks(user["id"], payload.ids, remove=False)
    audit(user["id"], "spotify_tracks_saved", {"count": len(payload.ids)}, client_ip(request))
    return result


@app.delete("/api/spotify/saved-tracks")
async def api_spotify_remove_tracks(payload: SpotifySavedTracksRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "spotify.control")
    data = payload.model_dump()
    require_confirmation(request, user, "spotify.saved_tracks.remove", data)
    result = await spotify_save_tracks(user["id"], payload.ids, remove=True)
    audit(user["id"], "spotify_tracks_removed", {"count": len(payload.ids)}, client_ip(request))
    return result


class SpotifyPlaybackRequest(BaseModel):
    action: Literal["play", "pause", "next", "previous"]
    device_id: str | None = None
    uri: str | None = None


@app.post("/api/spotify/playback")
async def api_spotify_playback(payload: SpotifyPlaybackRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "spotify.control")
    data = payload.model_dump()
    require_confirmation(request, user, "spotify.playback", data)
    result = await spotify_playback(user["id"], **data)
    audit(user["id"], "spotify_playback", data, client_ip(request))
    return result


class SpotifyPlaylistRequest(BaseModel):
    name: str
    description: str = ""
    public: bool = False


@app.post("/api/spotify/playlists")
async def api_spotify_create_playlist(payload: SpotifyPlaylistRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "spotify.control")
    data = payload.model_dump()
    require_confirmation(request, user, "spotify.playlist.create", data)
    result = await spotify_create_playlist(user["id"], **data)
    audit(user["id"], "spotify_playlist_created", {"playlist_id": result.get("id"), "name": payload.name}, client_ip(request))
    return result


@app.get("/api/tiktok/user")
async def api_tiktok_user(user=Depends(current_user)):
    require_capability(user, "tiktok.read")
    return await tiktok_user(user["id"])


@app.get("/api/tiktok/videos")
async def api_tiktok_videos(max_count: int = 20, user=Depends(current_user)):
    require_capability(user, "tiktok.read")
    return await tiktok_videos(user["id"], max_count)


@app.get("/api/tiktok/creator-info")
async def api_tiktok_creator_info(user=Depends(current_user)):
    require_capability(user, "tiktok.publish")
    return await tiktok_creator_info(user["id"])


class TikTokPublishRequest(BaseModel):
    media_path: str
    mode: Literal["draft", "direct"] = "draft"
    title: str = ""
    privacy_level: str = "SELF_ONLY"
    disable_duet: bool = False
    disable_comment: bool = False
    disable_stitch: bool = False
    is_aigc: bool = False


@app.post("/api/tiktok/publish")
async def api_tiktok_publish(payload: TikTokPublishRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "tiktok.publish")
    data = payload.model_dump()
    require_confirmation(request, user, "tiktok.publish", data)
    result = await tiktok_publish(user["id"], **data)
    audit(user["id"], "tiktok_publish_initialized", {"mode": payload.mode, "publish_id": result.get("data", {}).get("publish_id")}, client_ip(request))
    return result


@app.get("/api/tiktok/status/{publish_id}")
async def api_tiktok_status(publish_id: str, user=Depends(current_user)):
    require_capability(user, "tiktok.read")
    return await tiktok_publish_status(user["id"], publish_id)


@app.get("/api/instagram/profile")
async def api_instagram_profile(user=Depends(current_user)):
    require_capability(user, "instagram.read")
    return await instagram_profile(user["id"])


@app.get("/api/instagram/media")
async def api_instagram_media(limit: int = 20, user=Depends(current_user)):
    require_capability(user, "instagram.read")
    return await instagram_media(user["id"], limit)


class InstagramPublishRequest(BaseModel):
    media_url: str = Field(min_length=8, max_length=2000, pattern=r"^https://.+")
    caption: str = Field(default="", max_length=2200)
    is_video: bool = False


@app.post("/api/instagram/publish")
async def api_instagram_publish(payload: InstagramPublishRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "instagram.publish")
    data = payload.model_dump()
    require_confirmation(request, user, "instagram.publish", data)
    result = await instagram_publish(user["id"], payload.media_url, payload.caption, is_video=payload.is_video)
    audit(user["id"], "instagram_published", {"is_video": payload.is_video, "media_id": result.get("id")}, client_ip(request))
    return result


@app.get("/api/homeassistant/states")
async def api_ha_states(entity_id: str | None = None, user=Depends(current_user)):
    require_capability(user, "homeassistant.read")
    return await homeassistant_request("GET", f"/api/states/{entity_id}" if entity_id else "/api/states")


@app.get("/api/homeassistant/services")
async def api_ha_services(user=Depends(current_user)):
    require_capability(user, "homeassistant.read")
    return await homeassistant_request("GET", "/api/services")


class HAServiceRequest(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9_]+$")
    service: str = Field(pattern=r"^[a-z0-9_]+$")
    service_data: dict[str, Any] = Field(default_factory=dict)
    return_response: bool = False


@app.post("/api/homeassistant/service")
async def api_ha_service(payload: HAServiceRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "homeassistant.control")
    cfg = get_app_config("homeassistant") or {}
    allowed_domains = set(cfg.get("allowed_domains") or ["light", "switch", "media_player", "cover", "lock", "climate", "scene", "script"])
    if payload.domain not in allowed_domains:
        raise HTTPException(403, f"Home Assistant domain is not allowed: {payload.domain}")
    if payload.domain == "homeassistant" and payload.service in {"stop", "restart"}:
        raise HTTPException(403, "Home Assistant stop/restart is blocked by ATHENA")
    data = payload.model_dump()
    require_confirmation(request, user, "homeassistant.service", data)
    suffix = "?return_response=true" if payload.return_response else ""
    result = await homeassistant_request("POST", f"/api/services/{payload.domain}/{payload.service}{suffix}", json=payload.service_data)
    audit(user["id"], "homeassistant_service", {"domain": payload.domain, "service": payload.service, "data": payload.service_data}, client_ip(request))
    return result


@app.get("/api/emby/sessions")
async def api_emby_sessions(user=Depends(current_user)):
    require_capability(user, "emby.read")
    return await emby_request("GET", "/Sessions")


@app.get("/api/emby/items")
async def api_emby_items(parent_id: str | None = None, search_term: str | None = None, limit: int = 50, user=Depends(current_user)):
    require_capability(user, "emby.read")
    cfg = get_app_config("emby") or {}
    user_id = cfg.get("user_id")
    if not user_id:
        raise IntegrationError("not_configured", "Emby user_id is missing from the system configuration", http_status=409)
    params: dict[str, Any] = {"Recursive": "true", "Limit": max(1, min(limit, 200)), "Fields": "Path,Overview,MediaSources"}
    if parent_id:
        params["ParentId"] = parent_id
    if search_term:
        params["SearchTerm"] = search_term
    return await emby_request("GET", f"/Users/{user_id}/Items", params=params)


class EmbyControlRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    command: Literal["PlayPause", "Stop", "Unpause", "Pause", "NextTrack", "PreviousTrack"]


@app.post("/api/emby/control")
async def api_emby_control(payload: EmbyControlRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "emby.control")
    data = payload.model_dump()
    require_confirmation(request, user, "emby.control", data)
    result = await emby_request("POST", f"/Sessions/{payload.session_id}/Playing/{payload.command}")
    audit(user["id"], "emby_control", data, client_ip(request))
    return result


class CallRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./@:+-]+$")
    extension: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_*#+-]+$")
    context: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    priority: int = Field(default=1, ge=1, le=100)
    timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    caller_id: str | None = Field(default=None, max_length=120, pattern=r"^[^\r\n]+$")


@app.post("/api/voip/call")
async def api_voip_call(payload: CallRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voip.call")
    data = payload.model_dump(exclude_none=True)
    require_confirmation(request, user, "voip.call", data)
    result = await asterisk_originate(data)
    audit(user["id"], "voip_call_originated", {"channel": payload.channel, "extension": payload.extension, "response": result.get("Response")}, client_ip(request))
    return result

@app.get("/family", response_class=HTMLResponse)
async def family_page(request: Request, user=Depends(current_user)):
    require_capability(user, "family.tasks.read")
    with connect() as conn:
        tasks = conn.execute(
            """SELECT t.*,u.display_name AS assigned_name FROM family_tasks t LEFT JOIN users u ON u.id=t.assigned_to
               WHERE t.created_by=? OR t.assigned_to=? ORDER BY t.created_at DESC LIMIT 100""",
            (user["id"], user["id"]),
        ).fetchall()
        family = conn.execute("SELECT id,display_name,role FROM users WHERE active=1 ORDER BY display_name").fetchall()
    rows = "".join(f"<tr><td>{esc(t['title'])}</td><td>{esc(t['assigned_name'] or '-')}</td><td>{esc(t['status'])}</td><td>{esc(t['due_at'] or '')}</td></tr>" for t in tasks)
    options = "".join(f"<option value='{esc(m['id'])}'>{esc(m['display_name'])}</option>" for m in family)
    create = ""
    if is_allowed(user, "family.tasks.write"):
        create = f"""<div class='card'><h2>Νέα οικογενειακή εργασία</h2><form method='post' action='/family/tasks'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Τίτλος<input name='title' required></label><label>Σημειώσεις<textarea name='notes'></textarea></label><label>Ανάθεση<select name='assigned_to'><option value=''>Χωρίς ανάθεση</option>{options}</select></label><label>Προθεσμία<input name='due_at' type='datetime-local'></label><button>Δημιουργία</button></form></div>"""
    location = ""
    if is_allowed(user, "location.share"):
        location = f"""<div class='card'><h2>Κοινοποίηση τοποθεσίας</h2><label class='inline'><input id='locEnabled' type='checkbox'>Ενεργή συγκατάθεση</label><label class='inline'><input id='locFamily' type='checkbox'>Κοινοποίηση στην οικογένεια</label><button onclick='saveConsent()'>Αποθήκευση συγκατάθεσης</button><button onclick='sendLocation()'>Αποστολή τρέχουσας τοποθεσίας</button><pre id='locOut'></pre></div><script>
        async function saveConsent(){{let d=await api('/api/location/consent',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled:locEnabled.checked,share_with_family:locFamily.checked,retention_hours:24}})}});locOut.textContent=pretty(d)}}
        function sendLocation(){{navigator.geolocation.getCurrentPosition(async p=>{{try{{let d=await api('/api/location',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{latitude:p.coords.latitude,longitude:p.coords.longitude,accuracy:p.coords.accuracy,source:'browser'}})}});locOut.textContent=pretty(d)}}catch(e){{locOut.textContent=e.message}}}},e=>locOut.textContent=e.message,{{enableHighAccuracy:true}})}}
        </script>"""
    body = f"<h1>Οικογένεια</h1><div class='card'><table><tr><th>Εργασία</th><th>Ανάθεση</th><th>Κατάσταση</th><th>Προθεσμία</th></tr>{rows or '<tr><td colspan=4>Δεν υπάρχουν εργασίες.</td></tr>'}</table></div>{create}{location}"
    return layout("Οικογένεια", body, user, user["csrf_token"])


@app.post("/family/tasks")
async def family_task_form(request: Request, title: str = Form(...), notes: str = Form(""), assigned_to: str = Form(""), due_at: str = Form(""), csrf: str = Form(...), user=Depends(current_user)):
    verify_csrf(request, user, csrf)
    require_capability(user, "family.tasks.write")
    task_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        if assigned_to and not conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (assigned_to,)).fetchone():
            raise HTTPException(400, "Assigned family member does not exist or is inactive")
        conn.execute("INSERT INTO family_tasks(id,created_by,assigned_to,title,notes,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (task_id, user["id"], assigned_to or None, title, notes, due_at or None, now, now))
    audit(user["id"], "family_task_created", {"task_id": task_id, "assigned_to": assigned_to or None}, client_ip(request))
    return RedirectResponse("/family", 303)


class FamilyTaskRequest(BaseModel):
    title: str
    notes: str = ""
    assigned_to: str | None = None
    due_at: str | None = None


@app.get("/api/family/tasks")
async def api_family_tasks(status: str | None = None, user=Depends(current_user)):
    require_capability(user, "family.tasks.read")
    with connect() as conn:
        if status:
            rows = conn.execute("SELECT * FROM family_tasks WHERE (created_by=? OR assigned_to=?) AND status=? ORDER BY created_at DESC", (user["id"], user["id"], status)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM family_tasks WHERE created_by=? OR assigned_to=? ORDER BY created_at DESC", (user["id"], user["id"])).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/family/tasks")
async def api_family_task_create(payload: FamilyTaskRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "family.tasks.write")
    task_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        if payload.assigned_to and not conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (payload.assigned_to,)).fetchone():
            raise HTTPException(400, "Assigned family member does not exist or is inactive")
        conn.execute("INSERT INTO family_tasks(id,created_by,assigned_to,title,notes,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (task_id, user["id"], payload.assigned_to, payload.title, payload.notes, payload.due_at, now, now))
    audit(user["id"], "family_task_created", {"task_id": task_id}, client_ip(request))
    return {"id": task_id, "status": "open"}


class FamilyTaskUpdate(BaseModel):
    status: Literal["open", "in_progress", "done", "cancelled"]


@app.patch("/api/family/tasks/{task_id}")
async def api_family_task_update(task_id: str, payload: FamilyTaskUpdate, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "family.tasks.write")
    now = utcnow()
    completed = now if payload.status == "done" else None
    with connect() as conn:
        cur = conn.execute("UPDATE family_tasks SET status=?,updated_at=?,completed_at=? WHERE id=? AND (created_by=? OR assigned_to=?)", (payload.status, now, completed, task_id, user["id"], user["id"]))
    if cur.rowcount == 0:
        raise HTTPException(404, "Task not found")
    audit(user["id"], "family_task_updated", {"task_id": task_id, "status": payload.status}, client_ip(request))
    return {"status": payload.status}


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request, user=Depends(current_user)):
    require_capability(user, "memory.private")
    body = """<h1>Σημασιολογική μνήμη</h1><div class='card'><label>Αναζήτηση<input id='mq'></label><button onclick='ms()'>Αναζήτηση</button><pre id='mr'></pre></div><div class='card'><label>Νέα μνήμη<textarea id='mt'></textarea></label><label>Χώρος<select id='mn'><option>private</option><option>family_shared</option><option>project_shared</option></select></label><button onclick='ma()'>Αποθήκευση</button><pre id='mo'></pre></div><script>async function ms(){try{mr.textContent=pretty(await api('/api/memory/search?q='+encodeURIComponent(mq.value)))}catch(e){mr.textContent=e.message}}async function ma(){try{mo.textContent=pretty(await api('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:mt.value,namespace:mn.value,metadata:{source:'web'}})}))}catch(e){mo.textContent=e.message}}</script>"""
    return layout("Μνήμη", body, user, user["csrf_token"])


class MemoryCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    namespace: Literal["private", "family_shared", "project_shared", "system"] = "private"
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/memory")
async def api_memory_create(payload: MemoryCreateRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    capability = {
        "private": "memory.private",
        "family_shared": "memory.family",
        "project_shared": "memory.project",
        "system": "memory.system",
    }[payload.namespace]
    require_capability(user, capability)
    owner = None if payload.namespace == "system" else user["id"]
    result = await asyncio.to_thread(add_memory, owner, payload.namespace, payload.text, payload.metadata)
    audit(user["id"], "memory_created", {"memory_id": result["id"], "namespace": payload.namespace}, client_ip(request))
    return result


@app.get("/api/memory/search")
async def api_memory_search(q: str, limit: int = 8, user=Depends(current_user)):
    require_capability(user, "memory.private")
    namespaces = ["private"]
    if is_allowed(user, "memory.family"):
        namespaces.append("family_shared")
    if is_allowed(user, "memory.project"):
        namespaces.append("project_shared")
    if is_allowed(user, "memory.system"):
        namespaces.append("system")
    return {"items": await asyncio.to_thread(search_memory, user["id"], q, namespaces, limit)}


@app.delete("/api/memory/{memory_id}")
async def api_memory_delete(memory_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "memory.private")
    require_confirmation(request, user, "memory.delete", {"memory_id": memory_id})
    if not delete_memory(user["id"], memory_id, user["role"] == "admin"):
        raise HTTPException(404, "Memory not found")
    audit(user["id"], "memory_deleted", {"memory_id": memory_id}, client_ip(request))
    return {"status": "deleted"}


@app.get("/location", response_class=HTMLResponse)
async def location_page(request: Request, user=Depends(current_user)):
    require_capability(user, "location.share")
    with connect() as conn:
        consent = conn.execute("SELECT * FROM location_consent WHERE user_id=?", (user["id"],)).fetchone()
        latest = conn.execute("SELECT * FROM locations WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user["id"],)).fetchone()
    enabled = bool(consent and consent["enabled"])
    family = bool(consent and consent["share_with_family"])
    retention = int(consent["retention_hours"] if consent else 24)
    latest_text = esc(dict(latest) if latest else "Δεν υπάρχει αποθηκευμένη τοποθεσία")
    body = f"""<h1>Τοποθεσία και συγκατάθεση</h1><div class='card'><p>Η τοποθεσία αποθηκεύεται μόνο αφού ενεργοποιήσεις ρητά τη συγκατάθεση. Η κοινοποίηση στην οικογένεια είναι ξεχωριστή επιλογή.</p>
    <label class='inline'><input id='locEnabled' type='checkbox' {'checked' if enabled else ''}>Ενεργή συγκατάθεση</label>
    <label class='inline'><input id='locFamily' type='checkbox' {'checked' if family else ''}>Κοινοποίηση τελευταίας τοποθεσίας στην οικογένεια</label>
    <label>Διατήρηση σε ώρες<input id='locRetention' type='number' min='1' max='8760' value='{retention}'></label>
    <button onclick='saveConsent()'>Αποθήκευση συγκατάθεσης</button><button onclick='sendLocation()'>Αποστολή τρέχουσας τοποθεσίας</button><pre id='locOut'>{latest_text}</pre></div>
    <script>
    async function saveConsent(){{try{{locOut.textContent=pretty(await api('/api/location/consent',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled:locEnabled.checked,share_with_family:locFamily.checked,retention_hours:Number(locRetention.value)}})}}))}}catch(e){{locOut.textContent=e.message}}}}
    function sendLocation(){{if(!navigator.geolocation){{locOut.textContent='Geolocation is not supported';return}}navigator.geolocation.getCurrentPosition(async p=>{{try{{locOut.textContent=pretty(await api('/api/location',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{latitude:p.coords.latitude,longitude:p.coords.longitude,accuracy:p.coords.accuracy,source:'browser'}})}}))}}catch(e){{locOut.textContent=e.message}}}},e=>locOut.textContent=e.message,{{enableHighAccuracy:true,maximumAge:0,timeout:20000}})}}
    </script>"""
    return layout("Τοποθεσία", body, user, user["csrf_token"])


class ConsentRequest(BaseModel):
    enabled: bool
    share_with_family: bool = False
    retention_hours: int = Field(default=24, ge=1, le=8760)


@app.post("/api/location/consent")
async def api_location_consent(payload: ConsentRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "location.share")
    with connect() as conn:
        conn.execute("""INSERT INTO location_consent(user_id,enabled,share_with_family,retention_hours,updated_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,share_with_family=excluded.share_with_family,retention_hours=excluded.retention_hours,updated_at=excluded.updated_at""",
                     (user["id"], int(payload.enabled), int(payload.share_with_family), payload.retention_hours, utcnow()))
        if not payload.enabled:
            conn.execute("DELETE FROM locations WHERE user_id=?", (user["id"],))
    audit(user["id"], "location_consent_updated", payload.model_dump(), client_ip(request))
    return {"status": "ready" if payload.enabled else "disabled", **payload.model_dump()}


class LocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    source: str = "browser"


@app.post("/api/location")
async def api_location(payload: LocationRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "location.share")
    with connect() as conn:
        consent = conn.execute("SELECT * FROM location_consent WHERE user_id=?", (user["id"],)).fetchone()
        if not consent or not consent["enabled"]:
            raise HTTPException(403, "Location consent is not enabled")
        location_id = str(uuid.uuid4())
        conn.execute("INSERT INTO locations(id,user_id,latitude,longitude,accuracy,source,created_at) VALUES(?,?,?,?,?,?,?)", (location_id, user["id"], payload.latitude, payload.longitude, payload.accuracy, payload.source, utcnow()))
    cleanup_expired()
    audit(user["id"], "location_updated", {"location_id": location_id, "source": payload.source}, client_ip(request))
    return {"status": "stored", "id": location_id}


@app.get("/api/family/locations")
async def api_family_locations(user=Depends(current_user)):
    require_capability(user, "family.locations.read")
    with connect() as conn:
        rows = conn.execute("""SELECT l.*,u.display_name FROM locations l JOIN users u ON u.id=l.user_id JOIN location_consent c ON c.user_id=l.user_id
                             WHERE c.enabled=1 AND c.share_with_family=1 AND l.created_at=(SELECT MAX(l2.created_at) FROM locations l2 WHERE l2.user_id=l.user_id)""").fetchall()
    return {"items": [dict(row) for row in rows]}

def user_media_rows(user_id: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM media_files WHERE owner_id=? ORDER BY created_at DESC", (user_id,)).fetchall()


@app.get("/media-library", response_class=HTMLResponse)
async def media_library(request: Request, user=Depends(current_user)):
    rows = user_media_rows(user["id"])
    table = "".join(
        f"<tr><td>{esc(row['original_name'])}</td><td>{esc(row['media_type'])}</td><td>{int(row['size_bytes'])}</td><td><code>{esc(row['relative_path'])}</code></td><td><button class='danger' onclick=\"removeMedia('{esc(row['id'])}')\">Διαγραφή</button></td></tr>"
        for row in rows
    ) or "<tr><td colspan='5'>Δεν υπάρχουν αρχεία.</td></tr>"
    body = f"""<h1>Προσωπική βιβλιοθήκη media</h1>
    <div class='card'><p>Κάθε αρχείο ανήκει αποκλειστικά στον τρέχοντα χρήστη. YouTube και TikTok μπορούν να χρησιμοποιήσουν μόνο αρχεία αυτής της βιβλιοθήκης.</p>
    <form method='post' action='/media-library' enctype='multipart/form-data'><input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Αρχείο media<input type='file' name='media' required accept='audio/*,video/*,image/*'></label><button>Μεταφόρτωση</button></form></div>
    <div class='card'><table><tr><th>Όνομα</th><th>Τύπος</th><th>Bytes</th><th>Path</th><th></th></tr>{table}</table></div>
    <script>async function removeMedia(id){{try{{await confirmed('media.delete',{{media_id:id}},async token=>{{await api('/api/media/'+id,{{method:'DELETE',headers:{{'X-ATHENA-Confirmation':token}}}});location.reload()}})}}catch(e){{alert(e.message)}}}}</script>"""
    return layout("Media", body, user, user["csrf_token"])


@app.post("/media-library")
async def media_upload(request: Request, media: UploadFile = File(...), csrf: str = Form(...), user=Depends(current_user)):
    verify_csrf(request, user, csrf)
    original = Path(media.filename or "media.bin").name
    suffix = Path(original).suffix.lower()[:12]
    allowed = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".jpg", ".jpeg", ".png", ".webp"}
    if suffix not in allowed:
        raise HTTPException(400, "Unsupported media file type")
    user_dir = MEDIA_DIR / "users" / user["id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / f"{uuid.uuid4().hex}{suffix}"
    max_bytes = int(get_system_setting("max_media_upload_bytes", 10 * 1024 * 1024 * 1024))
    total = 0
    try:
        with target.open("wb") as handle:
            while chunk := await media.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Media file exceeds the configured upload limit")
                handle.write(chunk)
        if total == 0:
            raise HTTPException(400, "Media file is empty")
        relative = target.relative_to(MEDIA_DIR).as_posix()
        media_type = media.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream"
        media_id = str(uuid.uuid4())
        with connect() as conn:
            conn.execute("INSERT INTO media_files(id,owner_id,original_name,relative_path,media_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?)", (media_id, user["id"], original, relative, media_type, total, utcnow()))
        audit(user["id"], "media_uploaded", {"media_id": media_id, "name": original, "size_bytes": total}, client_ip(request))
        return RedirectResponse("/media-library", 303)
    except Exception:
        target.unlink(missing_ok=True)
        raise


@app.delete("/api/media/{media_id}")
async def media_delete(media_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_confirmation(request, user, "media.delete", {"media_id": media_id})
    with connect() as conn:
        row = conn.execute("SELECT * FROM media_files WHERE id=? AND owner_id=?", (media_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Media file not found")
        conn.execute("DELETE FROM media_files WHERE id=?", (media_id,))
    path = (MEDIA_DIR / row["relative_path"]).resolve()
    if MEDIA_DIR.resolve() in path.parents:
        path.unlink(missing_ok=True)
    audit(user["id"], "media_deleted", {"media_id": media_id}, client_ip(request))
    return {"status": "deleted"}


@app.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request, user=Depends(current_user)):
    media_options = "".join(f"<option value='{esc(row['relative_path'])}'>{esc(row['original_name'])}</option>" for row in user_media_rows(user["id"]) if str(row["media_type"]).startswith("video/"))
    if not media_options:
        media_options = "<option value=''>Μεταφόρτωσε πρώτα video στη βιβλιοθήκη media</option>"
    body = """<h1>Ενέργειες</h1>
    <div class='card'><h2>Προτεινόμενες ενέργειες από την ATHENA</h2><p>Καμία πρόταση δεν εκτελείται χωρίς τον κωδικό σου και επιβεβαίωση δεμένη με το ακριβές payload.</p><button onclick='loadProposals()'>Ανανέωση</button><div id='proposals'></div></div>
    <div class='card'><h2>Ανάγνωση υπηρεσιών</h2><button onclick="showGet('/api/gmail/messages')">Gmail</button> <button onclick="showGet('/api/calendar/events')">Calendar</button> <button onclick="showGet('/api/google-tasks')">Tasks</button> <button onclick="showGet('/api/youtube/channel')">YouTube</button> <button onclick="showGet('/api/spotify/current')">Spotify</button> <button onclick="showGet('/api/tiktok/user')">TikTok</button> <button onclick="showGet('/api/homeassistant/states')">Home Assistant</button> <button onclick="showGet('/api/emby/sessions')">Emby</button><pre id='actionOut'></pre></div>
    <div class='grid'>
      <div class='card'><h2>Αποστολή Gmail</h2><input id='gmTo' placeholder='Παραλήπτης'><input id='gmSubject' placeholder='Θέμα'><textarea id='gmBody' placeholder='Μήνυμα'></textarea><button onclick='sendGmail()'>Επιβεβαίωση και αποστολή</button></div>
      <div class='card'><h2>Νέο Calendar event</h2><input id='calSummary' placeholder='Τίτλος'><input id='calStart' type='datetime-local'><input id='calEnd' type='datetime-local'><textarea id='calDescription' placeholder='Περιγραφή'></textarea><button onclick='createCalendar()'>Επιβεβαίωση και δημιουργία</button></div>
      <div class='card'><h2>YouTube upload</h2><select id='ytMedia'>__MEDIA_OPTIONS__</select><input id='ytTitle' placeholder='Τίτλος'><textarea id='ytDescription' placeholder='Περιγραφή'></textarea><select id='ytPrivacy'><option>private</option><option>unlisted</option><option>public</option></select><button onclick='uploadYouTube()'>Επιβεβαίωση και upload</button></div>
      <div class='card'><h2>TikTok publish</h2><select id='ttMedia'>__MEDIA_OPTIONS__</select><input id='ttTitle' placeholder='Caption'><select id='ttMode'><option value='draft'>draft</option><option value='direct'>direct</option></select><button onclick='publishTikTok()'>Επιβεβαίωση και publish</button></div>
      <div class='card'><h2>Spotify control</h2><select id='spAction'><option>play</option><option>pause</option><option>next</option><option>previous</option></select><input id='spDevice' placeholder='Device ID (προαιρετικό)'><input id='spUri' placeholder='Spotify URI (προαιρετικό)'><button onclick='spotifyControl()'>Επιβεβαίωση και εκτέλεση</button></div>
      <div class='card'><h2>Home Assistant service</h2><input id='haDomain' placeholder='Domain π.χ. light'><input id='haService' placeholder='Service π.χ. turn_on'><textarea id='haData' placeholder='JSON π.χ. {"entity_id":"light.salon"}'>{}</textarea><button onclick='haCall()'>Επιβεβαίωση και εκτέλεση</button></div>
      <div class='card'><h2>Emby control</h2><input id='embySession' placeholder='Session ID'><select id='embyCommand'><option>PlayPause</option><option>Pause</option><option>Unpause</option><option>Stop</option><option>NextTrack</option><option>PreviousTrack</option></select><button onclick='embyControl()'>Επιβεβαίωση και εκτέλεση</button></div>
      <div class='card'><h2>VoIP μέσω Asterisk</h2><input id='voipChannel' placeholder='Channel π.χ. PJSIP/100'><input id='voipExtension' placeholder='Extension'><input id='voipCaller' placeholder='Caller ID (προαιρετικό)'><button onclick='voipCall()'>Επιβεβαίωση και κλήση</button></div>
    </div>
    <script>
    function out(x){actionOut.textContent=pretty(x)} function fail(e){actionOut.textContent=e.message}
    function h(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
    async function loadProposals(){try{let d=await api('/api/actions/proposals');proposals.innerHTML=(d.items||[]).map(p=>`<div class="card"><strong>${h(p.action)}</strong><p>${h(p.summary||'')}</p><pre>${h(pretty(p.payload))}</pre><button onclick="executeProposal('${h(p.id)}')">Επιβεβαίωση και εκτέλεση</button> <button class="secondary" onclick="cancelProposal('${h(p.id)}')">Ακύρωση</button></div>`).join('')||'<p class="muted">Δεν υπάρχουν εκκρεμείς προτάσεις.</p>'}catch(e){proposals.textContent=e.message}}
    async function executeProposal(id){try{let p=await api('/api/actions/proposals/'+id);let action='proposal.execute:'+p.action;let confirmationPayload={proposal_id:id,action:p.action,payload:p.payload};await confirmed(action,confirmationPayload,async token=>{out(await api('/api/actions/proposals/'+id+'/execute',{method:'POST',headers:{'X-ATHENA-Confirmation':token}}));await loadProposals()})}catch(e){fail(e)}}
    async function cancelProposal(id){try{await api('/api/actions/proposals/'+id,{method:'DELETE'});await loadProposals()}catch(e){fail(e)}}
    async function showGet(url){try{out(await api(url))}catch(e){fail(e)}}
    async function runConfirmed(action,payload,url){try{await confirmed(action,payload,async token=>out(await api(url,{method:'POST',headers:{'Content-Type':'application/json','X-ATHENA-Confirmation':token},body:JSON.stringify(payload)})))}catch(e){fail(e)}}
    function sendGmail(){let p={to:gmTo.value,subject:gmSubject.value,body:gmBody.value,cc:'',bcc:''};runConfirmed('gmail.send',p,'/api/gmail/send')}
    function createCalendar(){let p={summary:calSummary.value,description:calDescription.value,location:'',start:{dateTime:new Date(calStart.value).toISOString()},end:{dateTime:new Date(calEnd.value).toISOString()},attendees:[]};runConfirmed('calendar.create',p,'/api/calendar/events')}
    function uploadYouTube(){let p={media_path:ytMedia.value,title:ytTitle.value,description:ytDescription.value,privacy_status:ytPrivacy.value,category_id:'22',tags:[]};runConfirmed('youtube.upload',p,'/api/youtube/upload')}
    function publishTikTok(){let p={media_path:ttMedia.value,mode:ttMode.value,title:ttTitle.value,privacy_level:'SELF_ONLY',disable_duet:false,disable_comment:false,disable_stitch:false,is_aigc:false};runConfirmed('tiktok.publish',p,'/api/tiktok/publish')}
    function spotifyControl(){let p={action:spAction.value,device_id:spDevice.value||null,uri:spUri.value||null};runConfirmed('spotify.playback',p,'/api/spotify/playback')}
    function haCall(){try{let p={domain:haDomain.value,service:haService.value,service_data:JSON.parse(haData.value||'{}'),return_response:false};runConfirmed('homeassistant.service',p,'/api/homeassistant/service')}catch(e){fail(e)}}
    function embyControl(){let p={session_id:embySession.value,command:embyCommand.value};runConfirmed('emby.control',p,'/api/emby/control')}
    function voipCall(){let p={channel:voipChannel.value,extension:voipExtension.value,priority:1,timeout_ms:30000};if(voipCaller.value)p.caller_id=voipCaller.value;runConfirmed('voip.call',p,'/api/voip/call')}
    loadProposals();
    </script>""".replace("__MEDIA_OPTIONS__", media_options)
    return layout("Ενέργειες", body, user, user["csrf_token"])



@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request, user=Depends(current_user)):
    require_capability(user, "creative.read")
    shell = f"""<!doctype html><html lang='el'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Γράφος · ATHENA</title>
    <link rel='stylesheet' href='/static/styles.css'>
    </head><body>
    <canvas id='graphCanvas'></canvas>
    <div id='inspector' class='panel'>
      <div id='inspectorBody'><p class='empty'>Φόρτωση…</p></div>
      <div id='hubs'><h3>Top hubs</h3><ol id='hubsList'></ol></div>
    </div>
    <div id='rightPanel' class='panel'>
      <div id='reactor' class='idle'><img src='/static/avatar.jpg' alt='ATHENA'><canvas id='reactorWave'></canvas></div>
      <div id='reactorLabel'>idle</div>
      <div id='modelLabel'></div>
      <div id='filterList'></div>
      <button id='refreshButton' style='width:100%;margin-top:10px;background:var(--card-2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px;cursor:pointer'>↻ Ανανέωση</button>
    </div>
    <div id='askBar' class='panel'>
      <button id='micButton' title='Μικρόφωνο'>🎙</button>
      <input id='askInput' type='text' autocomplete='off'>
      <button id='askSend' class='primary'>Ρώτα</button>
    </div>
    <div id='answerCard' class='panel'></div>
    <script>window.ATHENA_CSRF="{user['csrf_token']}";</script>
    <script src='/static/graph.js'></script>
    <script src='/static/app.js'></script>
    </body></html>"""
    return HTMLResponse(shell)


@app.get("/voice", response_class=HTMLResponse)
async def voice_page(request: Request, user=Depends(current_user)):
    require_capability(user, "voice.use")
    body = """<h1>Φωνή</h1>
    <div class='grid'>
      <div class='card'><h2>Speech-to-text</h2><input id='sttFile' type='file' accept='audio/*'><button onclick='stt()'>Μεταγραφή</button><pre id='sttOut'></pre></div>
      <div class='card'><h2>Text-to-speech</h2><textarea id='ttsText'></textarea><label>Piper voice<input id='ttsVoice' value='el_GR-rapunzelina-low'></label><button onclick='tts()'>Δημιουργία WAV</button><audio id='ttsAudio' controls></audio><pre id='ttsOut'></pre></div>
      <div class='card'><h2>Voice ID</h2><p>Η εγγραφή βιομετρικού αποτυπώματος είναι προαιρετική και γίνεται μόνο για τον τρέχοντα λογαριασμό.</p><input id='enrollFiles' type='file' accept='audio/*' multiple><button onclick='enroll()'>Εγγραφή Voice ID</button><input id='verifyFile' type='file' accept='audio/*'><button onclick='verifyVoice()'>Έλεγχος Voice ID</button><button class='danger' onclick='removeVoice()'>Διαγραφή Voice ID</button><pre id='voiceOut'></pre></div>
      <div class='card'><h2>Μικρόφωνο / Wake phrase</h2><label>Wake phrase<input id='wakePhrase' value='Αθηνά'></label><button onclick='recordWake()'>Ηχογράφηση 5 δευτερολέπτων</button><pre id='wakeOut'></pre><small>Η πρόσβαση μικροφώνου σε browser συνήθως απαιτεί HTTPS ή localhost.</small></div>
    </div>
    <script>
    async function stt(){let f=sttFile.files[0];if(!f)return;let fd=new FormData();fd.append('audio',f);try{sttOut.textContent=pretty(await api('/api/voice/stt',{method:'POST',body:fd}))}catch(e){sttOut.textContent=e.message}}
    async function tts(){try{let r=await fetch('/api/voice/tts',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.ATHENA_CSRF},body:JSON.stringify({text:ttsText.value,voice:ttsVoice.value,length_scale:1})});if(!r.ok)throw new Error(await r.text());let b=await r.blob();ttsAudio.src=URL.createObjectURL(b);ttsOut.textContent='ready'}catch(e){ttsOut.textContent=e.message}}
    async function enroll(){let fd=new FormData();for(let f of enrollFiles.files)fd.append('samples',f);fd.append('consent','true');try{voiceOut.textContent=pretty(await api('/api/voice/enroll',{method:'POST',body:fd}))}catch(e){voiceOut.textContent=e.message}}
    async function verifyVoice(){let f=verifyFile.files[0];if(!f)return;let fd=new FormData();fd.append('audio',f);try{voiceOut.textContent=pretty(await api('/api/voice/verify',{method:'POST',body:fd}))}catch(e){voiceOut.textContent=e.message}}
    async function removeVoice(){try{await confirmed('voice.enrollment.delete',{},async token=>voiceOut.textContent=pretty(await api('/api/voice/enrollment',{method:'DELETE',headers:{'X-ATHENA-Confirmation':token}})))}catch(e){voiceOut.textContent=e.message}}
    async function recordWake(){try{let s=await navigator.mediaDevices.getUserMedia({audio:true});let m=new MediaRecorder(s);let chunks=[];m.ondataavailable=e=>chunks.push(e.data);m.start();wakeOut.textContent='recording';setTimeout(()=>m.stop(),5000);m.onstop=async()=>{s.getTracks().forEach(t=>t.stop());let fd=new FormData();fd.append('audio',new Blob(chunks,{type:m.mimeType}),'wake.webm');fd.append('wake_phrase',wakePhrase.value);wakeOut.textContent=pretty(await api('/api/voice/wake',{method:'POST',body:fd}))}}catch(e){wakeOut.textContent=e.message}}
    </script>"""
    return layout("Φωνή", body, user, user["csrf_token"])


async def save_upload(upload: UploadFile, prefix: str) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix[:12]
    target = TEMP_DIR / f"{prefix}-{uuid.uuid4().hex}{suffix}"
    total = 0
    try:
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise HTTPException(413, "Audio upload exceeds 500 MiB")
                handle.write(chunk)
        if total == 0:
            raise HTTPException(400, "Uploaded audio is empty")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


@app.post("/api/voice/stt")
async def api_voice_stt(request: Request, audio: UploadFile = File(...), model: str = Form(""), language: str = Form(""), user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    path = await save_upload(audio, "stt")
    try:
        if language and not re.fullmatch(r"[A-Za-z-]{2,16}", language):
            raise HTTPException(400, "Invalid language code")
        if elevenlabs_configured():
            try:
                result = await elevenlabs_transcribe(path, language or None)
                audit(user["id"], "voice_transcribed", {"language": result.get("language"), "backend": "elevenlabs"}, client_ip(request))
                return result
            except VoiceBackendError as exc:
                logger.warning("ElevenLabs STT failed, falling back to local: %s", exc.message)
        cfg = get_system_setting("voice", {}) or {}
        selected_model = model or cfg.get("stt_model", "small")
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,120}", selected_model) or ".." in selected_model:
            raise HTTPException(400, "Invalid Whisper model name")
        result = await transcribe(path, model_name=selected_model, device=cfg.get("stt_device", "cpu"), compute_type=cfg.get("stt_compute_type", "int8"), language=language or None)
        audit(user["id"], "voice_transcribed", {"language": result.get("language"), "backend": "local"}, client_ip(request))
        return result
    finally:
        path.unlink(missing_ok=True)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    voice: str = Field(default="el_GR-rapunzelina-low", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    speaker_id: int | None = None
    length_scale: float = Field(default=1.0, ge=0.5, le=2.0)


@app.post("/api/voice/tts")
async def api_voice_tts(payload: TTSRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    if elevenlabs_configured():
        try:
            audio_bytes = await elevenlabs_synthesize(payload.text, payload.voice if payload.voice != "el_GR-rapunzelina-low" else None)
            audit(user["id"], "voice_synthesized", {"backend": "elevenlabs"}, client_ip(request))
            return Response(content=audio_bytes, media_type="audio/mpeg")
        except VoiceBackendError as exc:
            logger.warning("ElevenLabs TTS failed, falling back to local: %s", exc.message)
    path = await synthesize(payload.text, payload.voice, payload.speaker_id, payload.length_scale)
    audit(user["id"], "voice_synthesized", {"voice": payload.voice, "backend": "local"}, client_ip(request))
    return FileResponse(path, media_type="audio/wav", filename="athena.wav", background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/api/voice/enroll")
async def api_voice_enroll(request: Request, samples: list[UploadFile] = File(...), consent: bool = Form(...), threshold: float = Form(0.65), user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.enroll")
    if not consent:
        raise HTTPException(400, "Explicit biometric consent is required")
    if not 2 <= len(samples) <= 10:
        raise HTTPException(400, "Provide between 2 and 10 voice samples")
    if not 0.3 <= threshold <= 0.95:
        raise HTTPException(400, "Voice ID threshold must be between 0.30 and 0.95")
    paths = [await save_upload(sample, "voice-enroll") for sample in samples]
    try:
        result = await enroll_voice(user["id"], paths, threshold)
        audit(user["id"], "voice_id_enrolled", {"sample_count": len(paths), "threshold": threshold}, client_ip(request))
        return result
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


@app.post("/api/voice/verify")
async def api_voice_verify(request: Request, audio: UploadFile = File(...), user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    path = await save_upload(audio, "voice-verify")
    try:
        result = await verify_voice(user["id"], path)
        audit(user["id"], "voice_id_verified", {"verified": result.get("verified"), "score": result.get("score")}, client_ip(request))
        return result
    finally:
        path.unlink(missing_ok=True)


@app.delete("/api/voice/enrollment")
async def api_voice_remove(request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.enroll")
    require_confirmation(request, user, "voice.enrollment.delete", {})
    remove_voiceprint(user["id"])
    audit(user["id"], "voice_id_removed", {}, client_ip(request))
    return {"status": "deleted"}


@app.post("/api/voice/wake")
async def api_voice_wake(request: Request, audio: UploadFile = File(...), wake_phrase: str = Form("Αθηνά"), user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    path = await save_upload(audio, "wake")
    try:
        cfg = get_system_setting("voice", {}) or {}
        result = await transcribe(path, model_name=cfg.get("wake_model", "tiny"), device=cfg.get("stt_device", "cpu"), compute_type=cfg.get("stt_compute_type", "int8"), language=cfg.get("wake_language", "el"))
        heard = result.get("text", "")
        activated = wake_phrase.casefold() in heard.casefold()
        return {"status": "ready", "activated": activated, "wake_phrase": wake_phrase, "transcription": heard}
    finally:
        path.unlink(missing_ok=True)


class SatelliteTokenRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    room: str = Field(default="", max_length=80)


@app.get("/api/voice/satellite/tokens")
async def api_satellite_tokens_list(user=Depends(current_user)):
    require_capability(user, "voice.use")
    return {"items": list_satellite_tokens(user["id"])}


@app.post("/api/voice/satellite/tokens")
async def api_satellite_tokens_create(payload: SatelliteTokenRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    token = create_satellite_token(user["id"], payload.label, payload.room)
    audit(user["id"], "satellite_token_created", {"label": payload.label, "room": payload.room}, client_ip(request))
    # Shown exactly once — the server never stores or displays it again, same as an API key.
    return {"token": token, "label": payload.label, "room": payload.room, "note": "Save this now — it will not be shown again."}


@app.delete("/api/voice/satellite/tokens/{token_id}")
async def api_satellite_tokens_revoke(token_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "voice.use")
    ok = revoke_satellite_token(user["id"], token_id)
    audit(user["id"], "satellite_token_revoked", {"token_id": token_id, "found": ok}, client_ip(request))
    return {"status": "revoked" if ok else "not_found"}


@app.websocket("/ws/voice/satellite")
async def ws_voice_satellite(websocket: WebSocket, token: str = "", wake_phrase: str = "Αθηνά"):
    row = resolve_satellite_token(token)
    if not row:
        await websocket.close(code=4401)
        return
    user = {"id": row["id"], "display_name": row["display_name"], "role": row["role"]}
    await run_satellite_session(websocket, user, wake_phrase)


def probe_audio(path: Path) -> dict[str, Any]:
    completed = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name,bit_rate", "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise HTTPException(400, f"Audio validation failed: {completed.stderr[-500:]}")
    return json.loads(completed.stdout)


def probe_artwork(path: Path) -> dict[str, Any]:
    from PIL import Image
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            fmt = image.format
    except Exception as exc:
        raise HTTPException(400, "Artwork is not a valid image") from exc
    if width != height:
        raise HTTPException(400, "Artwork must be square")
    return {"width": width, "height": height, "format": fmt}


@app.get("/releases", response_class=HTMLResponse)
async def releases_page(request: Request, user=Depends(current_user)):
    require_capability(user, "distrokid.manage")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM release_workspaces WHERE owner_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    table_parts = []
    for row in rows:
        package_link = f"<a class='button' href='/api/distrokid/releases/{esc(row['id'])}/package'>Package</a>" if row["package_path"] else ""
        table_parts.append(f"<tr><td>{esc(row['artist'])}</td><td>{esc(row['title'])}</td><td>{esc(row['status'])}</td><td>{package_link}</td></tr>")
    table = "".join(table_parts) or "<tr><td colspan='4'>Δεν υπάρχουν releases.</td></tr>"
    metadata_example = '{"genre":"","language":"el","explicit":false,"release_date":"","songwriters":[]}'
    body = (
        f"<h1>DistroKid Release Manager</h1><div class='card'><p>Η ATHENA δημιουργεί και ελέγχει το release package. "
        f"Η τελική υποβολή γίνεται στον επίσημο λογαριασμό DistroKid.</p><a class='button' href='{DISTROKID_UPLOAD_URL}' "
        f"target='_blank' rel='noopener'>Άνοιγμα DistroKid</a></div>"
        f"<div class='card'><h2>Νέο release</h2><form method='post' action='/releases' enctype='multipart/form-data'>"
        f"<input type='hidden' name='csrf' value='{esc(user['csrf_token'])}'><label>Artist<input name='artist' required></label>"
        f"<label>Τίτλος release<input name='title' required></label><label>Audio tracks<input type='file' name='audio' required accept='audio/*' multiple></label>"
        f"<label>Artwork<input type='file' name='artwork' accept='image/*'></label><label>Metadata JSON<textarea name='metadata_json'>{esc(metadata_example)}</textarea></label>"
        f"<label class='inline'><input type='checkbox' name='rights_confirmed' value='true' required>Έχω τα δικαιώματα audio, artwork, samples και δηλωμένων δημιουργών.</label>"
        f"<button>Δημιουργία package</button></form></div><div class='card'><table><tr><th>Artist</th><th>Τίτλος</th><th>Status</th><th></th></tr>{table}</table></div>"
    )
    return layout("DistroKid", body, user, user["csrf_token"])


@app.post("/releases")
async def create_release_form(
    request: Request,
    artist: str = Form(...),
    title: str = Form(...),
    metadata_json: str = Form("{}"),
    rights_confirmed: bool = Form(False),
    csrf: str = Form(...),
    audio: list[UploadFile] = File(...),
    artwork: UploadFile | None = File(None),
    user=Depends(current_user),
):
    verify_csrf(request, user, csrf)
    require_capability(user, "distrokid.manage")
    artist = artist.strip()
    title = title.strip()
    if not artist or len(artist) > 200 or not title or len(title) > 200:
        raise HTTPException(400, "Artist and release title are required and must not exceed 200 characters")
    if not rights_confirmed:
        raise HTTPException(400, "Rights confirmation is required")
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Metadata must be valid JSON") from exc
    if not isinstance(metadata, dict):
        raise HTTPException(400, "Metadata must be a JSON object")
    if not 1 <= len(audio) <= 100:
        raise HTTPException(400, "A release must contain between 1 and 100 audio tracks")

    release_id = str(uuid.uuid4())
    directory = RELEASE_DIR / release_id
    directory.mkdir(parents=True, exist_ok=False)
    max_track_bytes = int(get_system_setting("max_release_track_bytes", 4 * 1024 * 1024 * 1024))
    max_artwork_bytes = int(get_system_setting("max_release_artwork_bytes", 50 * 1024 * 1024))
    metadata_tracks = metadata.get("tracks") if isinstance(metadata.get("tracks"), list) else []
    track_records: list[dict[str, Any]] = []
    try:
        for index, upload in enumerate(audio, start=1):
            original = Path(upload.filename or f"track-{index}.wav").name
            audio_suffix = Path(original).suffix.lower() or ".wav"
            if audio_suffix not in {".wav", ".flac", ".mp3", ".m4a", ".aiff", ".aif", ".wma"}:
                raise HTTPException(400, f"Unsupported DistroKid audio type: {audio_suffix}")
            audio_path = directory / f"{index:02d}-{uuid.uuid4().hex[:8]}{audio_suffix}"
            total = 0
            with audio_path.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_track_bytes:
                        raise HTTPException(413, f"Track {index} exceeds the configured upload limit")
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(400, f"Track {index} is empty")
            validation = probe_audio(audio_path)
            declared = metadata_tracks[index - 1] if index - 1 < len(metadata_tracks) and isinstance(metadata_tracks[index - 1], dict) else {}
            track_title = str(declared.get("title") or Path(original).stem).strip()
            if not track_title or len(track_title) > 200:
                raise HTTPException(400, f"Track {index} title is invalid")
            track_records.append({"id": str(uuid.uuid4()), "number": index, "title": track_title, "path": audio_path, "validation": validation, "metadata": declared})

        artwork_path: Path | None = None
        if artwork and artwork.filename:
            art_suffix = Path(artwork.filename).suffix.lower() or ".jpg"
            if art_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise HTTPException(400, "Artwork must be JPG, PNG or WebP")
            artwork_path = directory / f"artwork{art_suffix}"
            total = 0
            with artwork_path.open("wb") as handle:
                while chunk := await artwork.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_artwork_bytes:
                        raise HTTPException(413, "Artwork exceeds the configured upload limit")
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(400, "Artwork is empty")
        art_info = probe_artwork(artwork_path) if artwork_path else None
        manifest = {
            "release_id": release_id,
            "artist": artist,
            "title": title,
            "metadata": metadata,
            "tracks": [
                {"number": t["number"], "title": t["title"], "audio": t["path"].name, "validation": t["validation"], "metadata": t["metadata"]}
                for t in track_records
            ],
            "artwork": artwork_path.name if artwork_path else None,
            "artwork_validation": art_info,
            "rights_confirmed_by": user["id"],
            "created_at": utcnow(),
            "distribution_service": "DistroKid",
            "submission_mode": "manual_official_web",
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        package_path = directory / f"{release_id}.zip"
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for track in track_records:
                archive.write(track["path"], f"audio/{track['path'].name}")
            if artwork_path:
                archive.write(artwork_path, artwork_path.name)
            archive.write(manifest_path, "manifest.json")
        now = utcnow()
        with connect() as conn:
            conn.execute(
                "INSERT INTO release_workspaces(id,owner_id,title,artist,audio_path,artwork_path,metadata_json,status,package_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (release_id, user["id"], title, artist, str(track_records[0]["path"]), str(artwork_path) if artwork_path else None, json.dumps(metadata, ensure_ascii=False), "package_ready", str(package_path), now, now),
            )
            for track in track_records:
                conn.execute(
                    "INSERT INTO release_tracks(id,release_id,track_number,title,audio_path,validation_json,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (track["id"], release_id, track["number"], track["title"], str(track["path"]), json.dumps(track["validation"], ensure_ascii=False), json.dumps(track["metadata"], ensure_ascii=False), now),
                )
        audit(user["id"], "distrokid_package_created", {"release_id": release_id, "artist": artist, "title": title, "track_count": len(track_records)}, client_ip(request))
        return RedirectResponse("/releases", 303)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@app.get("/api/distrokid/releases/{release_id}/package")
async def download_release_package(release_id: str, user=Depends(current_user)):
    require_capability(user, "distrokid.manage")
    with connect() as conn:
        row = conn.execute("SELECT * FROM release_workspaces WHERE id=? AND owner_id=?", (release_id, user["id"])).fetchone()
    if not row or not row["package_path"] or not Path(row["package_path"]).is_file():
        raise HTTPException(404, "Release package not found")
    return FileResponse(row["package_path"], media_type="application/zip", filename=f"athena-release-{release_id}.zip")


def _proposal_view(row: sqlite3.Row) -> dict[str, Any]:
    stored = json.loads(row["payload_json"])
    payload = stored.get("payload", {})
    return {
        "id": row["id"],
        "action": row["action"],
        "payload": payload,
        "summary": stored.get("summary", ""),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "confirmation_action": f"proposal.execute:{row['action']}",
        "confirmation_payload": {"proposal_id": row["id"], "action": row["action"], "payload": payload},
    }


@app.get("/api/actions/proposals")
async def api_action_proposals(status: Literal["pending", "executed", "failed", "cancelled", "all"] = "pending", user=Depends(current_user)):
    with connect() as conn:
        if status == "all":
            rows = conn.execute("SELECT * FROM action_proposals WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (user["id"],)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM action_proposals WHERE user_id=? AND status=? ORDER BY created_at DESC LIMIT 200", (user["id"], status)).fetchall()
    return {"items": [_proposal_view(row) for row in rows]}


@app.get("/api/actions/proposals/{proposal_id}")
async def api_action_proposal(proposal_id: str, user=Depends(current_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM action_proposals WHERE id=? AND user_id=?", (proposal_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Action proposal not found")
    return _proposal_view(row)


@app.delete("/api/actions/proposals/{proposal_id}")
async def api_cancel_action_proposal(proposal_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM action_proposals WHERE id=? AND user_id=? AND status='pending'", (proposal_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Pending action proposal not found")
        conn.execute("UPDATE action_proposals SET status='cancelled',updated_at=? WHERE id=?", (utcnow(), proposal_id))
    audit(user["id"], "action_proposal_cancelled", {"proposal_id": proposal_id, "action": row["action"]}, client_ip(request))
    return {"status": "cancelled", "proposal_id": proposal_id}


async def _execute_proposed_action(user, action: str, payload: dict[str, Any]) -> Any:
    if action == "gmail.send":
        model = GmailSendRequest.model_validate(payload)
        return await gmail_send(user["id"], **model.model_dump())
    if action == "calendar.create":
        model = CalendarEventRequest.model_validate(payload)
        return await calendar_create(user["id"], model.model_dump(exclude_none=True))
    if action == "google_tasks.create":
        model = GoogleTaskRequest.model_validate(payload)
        return await tasks_create(user["id"], model.title, model.notes, model.due)
    if action == "youtube.upload":
        model = YouTubeUploadRequest.model_validate(payload)
        return await youtube_upload(user["id"], **model.model_dump())
    if action == "youtube.comment.reply":
        parent_id = str(payload.get("parent_id", ""))
        model = YouTubeCommentReplyRequest.model_validate({"text": payload.get("text", "")})
        return await youtube_reply_comment(user["id"], parent_id, model.text)
    if action == "spotify.playback":
        model = SpotifyPlaybackRequest.model_validate(payload)
        return await spotify_playback(user["id"], **model.model_dump())
    if action == "spotify.playlist.create":
        model = SpotifyPlaylistRequest.model_validate(payload)
        return await spotify_create_playlist(user["id"], **model.model_dump())
    if action in {"spotify.saved_tracks.add", "spotify.saved_tracks.remove"}:
        model = SpotifySavedTracksRequest.model_validate(payload)
        return await spotify_save_tracks(user["id"], model.ids, remove=action.endswith("remove"))
    if action == "tiktok.publish":
        model = TikTokPublishRequest.model_validate(payload)
        return await tiktok_publish(user["id"], **model.model_dump())
    if action == "homeassistant.service":
        model = HAServiceRequest.model_validate(payload)
        cfg = get_app_config("homeassistant") or {}
        allowed_domains = set(cfg.get("allowed_domains") or ["light", "switch", "media_player", "cover", "lock", "climate", "scene", "script"])
        if model.domain not in allowed_domains or (model.domain == "homeassistant" and model.service in {"stop", "restart"}):
            raise HTTPException(403, "Home Assistant action is blocked by the configured allowlist")
        suffix = "?return_response=true" if model.return_response else ""
        return await homeassistant_request("POST", f"/api/services/{model.domain}/{model.service}{suffix}", json=model.service_data)
    if action == "emby.control":
        model = EmbyControlRequest.model_validate(payload)
        return await emby_request("POST", f"/Sessions/{model.session_id}/Playing/{model.command}")
    if action == "voip.call":
        model = CallRequest.model_validate(payload)
        return await asterisk_originate(model.model_dump(exclude_none=True))
    raise HTTPException(400, "Unsupported action proposal")


@app.post("/api/actions/proposals/{proposal_id}/execute")
async def api_execute_action_proposal(proposal_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM action_proposals WHERE id=? AND user_id=? AND status='pending'", (proposal_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Pending action proposal not found")
    view = _proposal_view(row)
    capability = ACTION_CAPABILITIES.get(row["action"])
    if not capability:
        raise HTTPException(400, "Unsupported action proposal")
    require_capability(user, capability)
    require_confirmation(request, user, view["confirmation_action"], view["confirmation_payload"])
    try:
        result = await _execute_proposed_action(user, row["action"], view["payload"])
    except ValidationError as exc:
        error_result = {"status": "invalid_payload", "error": str(exc)}
        with connect() as conn:
            conn.execute("UPDATE action_proposals SET status='failed',result_json=?,updated_at=? WHERE id=?", (json.dumps(error_result, ensure_ascii=False), utcnow(), proposal_id))
        audit(user["id"], "action_proposal_failed", {"proposal_id": proposal_id, "action": row["action"], **error_result}, client_ip(request))
        raise HTTPException(422, detail=error_result) from exc
    except Exception as exc:
        error_result = {"status": getattr(exc, "status", "error"), "error": str(exc)}
        with connect() as conn:
            conn.execute("UPDATE action_proposals SET status='failed',result_json=?,updated_at=? WHERE id=?", (json.dumps(error_result, ensure_ascii=False), utcnow(), proposal_id))
        audit(user["id"], "action_proposal_failed", {"proposal_id": proposal_id, "action": row["action"], **error_result}, client_ip(request))
        raise
    with connect() as conn:
        conn.execute("UPDATE action_proposals SET status='executed',result_json=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False, default=str), utcnow(), proposal_id))
    audit(user["id"], "action_proposal_executed", {"proposal_id": proposal_id, "action": row["action"]}, client_ip(request))
    return {"status": "executed", "proposal_id": proposal_id, "action": row["action"], "result": result}


@app.get("/api/audit")
async def api_audit(limit: int = 200, user=Depends(current_user)):
    require_capability(user, "audit.read")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    return {"items": [dict(row) for row in rows]}


class NotificationChannelRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    server: str = Field(default="https://ntfy.sh", max_length=200, pattern=r"^https?://.+")


@app.get("/api/notifications/channels")
async def api_notify_channels_list(user=Depends(current_user)):
    require_capability(user, "notifications.manage")
    return {"items": list_notify_channels(user["id"])}


@app.post("/api/notifications/channels")
async def api_notify_channels_create(payload: NotificationChannelRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "notifications.manage")
    channel = add_notify_channel(user["id"], payload.topic, payload.server)
    audit(user["id"], "notification_channel_added", {"kind": "ntfy", "topic": payload.topic}, client_ip(request))
    return channel


@app.delete("/api/notifications/channels/{channel_id}")
async def api_notify_channels_delete(channel_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "notifications.manage")
    ok = remove_notify_channel(user["id"], channel_id)
    audit(user["id"], "notification_channel_removed", {"channel_id": channel_id, "found": ok}, client_ip(request))
    return {"status": "deleted" if ok else "not_found"}


@app.post("/api/notifications/test")
async def api_notify_test(request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "notifications.manage")
    results = await send_notification(user["id"], "ATHENA", "Δοκιμαστική ειδοποίηση — αν το βλέπεις, δουλεύει.")
    return {"results": results}


class RoutineRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    trigger_type: Literal["cron", "interval_minutes"]
    trigger_config: dict[str, Any]
    action_type: Literal["ask", "notify"]
    action_config: dict[str, Any]
    enabled: bool = True


@app.get("/api/routines")
async def api_routines_list(user=Depends(current_user)):
    require_capability(user, "routines.manage")
    return {"items": list_routines(user["id"])}


@app.post("/api/routines")
async def api_routines_create(payload: RoutineRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "routines.manage")
    try:
        routine = create_routine(user["id"], payload.title, payload.trigger_type, payload.trigger_config, payload.action_type, payload.action_config, enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    schedule_routine(routine)
    audit(user["id"], "routine_created", {"routine_id": routine["id"], "title": payload.title}, client_ip(request))
    return routine


class RoutineEnabledRequest(BaseModel):
    enabled: bool


@app.post("/api/routines/{routine_id}/enabled")
async def api_routines_set_enabled(routine_id: str, payload: RoutineEnabledRequest, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "routines.manage")
    ok = set_routine_enabled(user["id"], routine_id, payload.enabled, is_admin=user["role"] == "admin")
    if not ok:
        raise HTTPException(404, "Routine not found")
    if payload.enabled:
        routine = get_routine(routine_id)
        if routine:
            schedule_routine(routine)
    else:
        unschedule_routine(routine_id)
    audit(user["id"], "routine_enabled_changed", {"routine_id": routine_id, "enabled": payload.enabled}, client_ip(request))
    return {"status": "updated"}


@app.delete("/api/routines/{routine_id}")
async def api_routines_delete(routine_id: str, request: Request, user=Depends(current_user)):
    verify_csrf(request, user)
    require_capability(user, "routines.manage")
    ok = delete_routine_row(user["id"], routine_id, is_admin=user["role"] == "admin")
    unschedule_routine(routine_id)
    audit(user["id"], "routine_deleted", {"routine_id": routine_id, "found": ok}, client_ip(request))
    return {"status": "deleted" if ok else "not_found"}


class MCPServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _-]+$")
    transport: Literal["stdio", "http"]
    config: dict[str, Any]


@app.get("/api/mcp/servers")
async def api_mcp_servers_list(user=Depends(require_admin)):
    return {"items": list_mcp_servers()}


@app.post("/api/mcp/servers")
async def api_mcp_servers_create(payload: MCPServerRequest, request: Request, user=Depends(require_admin)):
    verify_csrf(request, user)
    try:
        server = add_mcp_server(payload.name, payload.transport, payload.config, user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await refresh_tool_cache(force=True)
    audit(user["id"], "mcp_server_added", {"name": payload.name, "transport": payload.transport}, client_ip(request))
    return server


class MCPServerEnabledRequest(BaseModel):
    enabled: bool


@app.post("/api/mcp/servers/{server_id}/enabled")
async def api_mcp_servers_set_enabled(server_id: str, payload: MCPServerEnabledRequest, request: Request, user=Depends(require_admin)):
    verify_csrf(request, user)
    ok = set_mcp_server_enabled(server_id, payload.enabled)
    if not ok:
        raise HTTPException(404, "MCP server not found")
    await refresh_tool_cache(force=True)
    audit(user["id"], "mcp_server_enabled_changed", {"server_id": server_id, "enabled": payload.enabled}, client_ip(request))
    return {"status": "updated"}


@app.delete("/api/mcp/servers/{server_id}")
async def api_mcp_servers_delete(server_id: str, request: Request, user=Depends(require_admin)):
    verify_csrf(request, user)
    ok = remove_mcp_server(server_id)
    await refresh_tool_cache(force=True)
    audit(user["id"], "mcp_server_removed", {"server_id": server_id, "found": ok}, client_ip(request))
    return {"status": "deleted" if ok else "not_found"}


@app.post("/api/mcp/servers/refresh")
async def api_mcp_servers_refresh(request: Request, user=Depends(require_admin)):
    verify_csrf(request, user)
    count = await refresh_tool_cache(force=True)
    return {"status": "refreshed", "tool_count": count}


@app.get("/api/graph/data")
async def api_graph_data(user=Depends(current_user)):
    """Feeds the knowledge-graph UI (/graph). Nodes are real rows the user can
    already see through other pages — creative projects, the platforms they
    touch, and this user's private memories — nothing invented for the graph."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    platform_labels = {"suno": "Suno", "higgsfield": "Higgsfield", "openart": "OpenArt", "other": "Άλλη πλατφόρμα"}
    platform_seen: set[str] = set()

    if is_allowed(user, "creative.read"):
        with connect() as conn:
            projects = conn.execute("SELECT * FROM creative_projects WHERE owner_id=? ORDER BY updated_at DESC", (user["id"],)).fetchall()
            for project in projects:
                prompts = conn.execute("SELECT platform FROM creative_prompts WHERE project_id=?", (project["id"],)).fetchall()
                platforms_used = sorted({p["platform"] for p in prompts})
                nodes.append({
                    "id": f"project:{project['id']}",
                    "type": "project",
                    "label": project["title"],
                    "status": project["status"],
                    "kind": project["kind"],
                    "connections": len(prompts) + len(platforms_used),
                })
                for platform in platforms_used:
                    platform_id = f"platform:{platform}"
                    platform_seen.add(platform)
                    edges.append({"source": f"project:{project['id']}", "target": platform_id})
    for platform in platform_seen:
        nodes.append({"id": f"platform:{platform}", "type": "platform", "label": platform_labels.get(platform, platform), "connections": sum(1 for e in edges if e["target"] == f"platform:{platform}")})

    if is_allowed(user, "memory.private"):
        with connect() as conn:
            memories = conn.execute("SELECT id,text,namespace,created_at FROM memories WHERE owner_id=? OR namespace!='private' ORDER BY created_at DESC LIMIT 80", (user["id"],)).fetchall()
        for memory in memories:
            text = memory["text"] or ""
            nodes.append({
                "id": f"memory:{memory['id']}",
                "type": "memory",
                "label": (text[:60] + "…") if len(text) > 60 else text,
                "namespace": memory["namespace"],
                "connections": 0,
            })

    return {"nodes": nodes, "edges": edges}


@app.post("/api/learning/reflect")
async def api_learning_reflect_now(request: Request, user=Depends(current_user)):
    """Manual trigger for this user's own daily reflection — normally runs at
    03:00 for everyone, this lets it be tested/used on demand instead of
    waiting for the clock."""
    verify_csrf(request, user)
    require_capability(user, "routines.manage")
    result = await run_daily_reflection(user["id"])
    audit(user["id"], "daily_reflection_triggered_manually", {"status": result.get("status")}, client_ip(request))
    return result
