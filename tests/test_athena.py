from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import wave
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME = Path(tempfile.mkdtemp(prefix="athena-tests-"))
os.environ["ATHENA_CONFIG_DIR"] = str(RUNTIME / "config")
os.environ["ATHENA_MEDIA_DIR"] = str(RUNTIME / "media")
os.environ["ATHENA_COOKIE_SECURE"] = "0"
os.environ.pop("ATHENA_PUBLIC_BASE_URL", None)

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import APP_VERSION, DB_PATH, MASTER_KEY_PATH, MEDIA_DIR, RELEASE_DIR, SESSION_COOKIE
from app.db import connect, init_db, utcnow
from app import cliproxy, integrations, memory, orchestrator
from app.integrations import (
    IntegrationError,
    _ami_packet,
    get_app_config,
    get_connection,
    public_base_url,
    require_connection_scopes,
    revoke_connection,
    safe_user_media_path,
    save_connection,
    set_app_config,
)
from app.main import app
from app.security import consume_confirmation, decrypt_json, issue_confirmation, password_hash, token_hash

ADMIN_PASSWORD = "Athena-Test-Admin-2026!"
CHILD_PASSWORD = "Athena-Test-Child-2026!"


@pytest.fixture(autouse=True)
def clean_runtime():
    for path in (RUNTIME / "config", RUNTIME / "media"):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
    memory._model = None
    yield


def bootstrap(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "setup_required"
    response = client.post(
        "/setup",
        data={"display_name": "Administrator", "username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert client.get("/health").json()["status"] == "ok"
    # Every other test in this file navigates straight to arbitrary pages
    # after bootstrap() — mark the persona wizard (added 2026-08-08, see
    # app/persona.py) as already done so persona_wizard_gate doesn't bounce
    # them all to /welcome. Tests that actually exercise the wizard itself
    # reset this explicitly.
    with connect() as conn:
        admin_row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    from app.persona import set_persona

    set_persona(str(admin_row["id"]), configured=True)


def csrf(client: TestClient) -> str:
    raw = client.cookies.get(SESSION_COOKIE)
    assert raw
    with connect() as conn:
        row = conn.execute("SELECT csrf_token FROM sessions WHERE token_hash=?", (token_hash(raw),)).fetchone()
    assert row
    return str(row["csrf_token"])


def admin_id() -> str:
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    assert row
    return str(row["id"])


def add_child(client: TestClient, *, username: str = "child") -> str:
    response = client.post(
        "/admin/users",
        data={"display_name": "Child", "username": username, "role": "child", "password": CHILD_PASSWORD, "csrf": csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    assert row
    from app.persona import set_persona  # see bootstrap()'s comment — same reasoning

    set_persona(str(row["id"]), configured=True)
    return str(row["id"])


def configure_minimal_apps(user_id: str) -> None:
    set_app_config("google", {"client_id": "google-client", "client_secret": "google-secret"}, user_id)
    set_app_config("spotify", {"client_id": "spotify-client"}, user_id)
    set_app_config("tiktok", {"client_key": "tiktok-key", "client_secret": "tiktok-secret"}, user_id)


def valid_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


def valid_png(size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_route_inventory_setup_and_truthful_initial_status() -> None:
    expected = {
        "/health", "/setup", "/login", "/account", "/admin/users", "/integrations",
        "/oauth/google/{service}/start", "/oauth/spotify/start", "/oauth/tiktok/start",
        "/admin/llm", "/api/llm/models", "/api/confirmations", "/api/ask", "/api/status", "/api/readiness",
        "/api/gmail/messages", "/api/gmail/messages/{message_id}", "/api/gmail/send",
        "/api/calendar/events", "/api/calendar/events/{event_id}",
        "/api/google-tasks", "/api/google-tasks/{task_id}",
        "/api/youtube/channel", "/api/youtube/comments/{video_id}",
        "/api/youtube/comments/{parent_id}/reply", "/api/youtube/analytics", "/api/youtube/upload",
        "/api/spotify/playlists", "/api/spotify/current", "/api/spotify/devices",
        "/api/spotify/saved-tracks", "/api/spotify/playback",
        "/api/tiktok/user", "/api/tiktok/videos", "/api/tiktok/creator-info",
        "/api/tiktok/publish", "/api/tiktok/status/{publish_id}",
        "/api/homeassistant/states", "/api/homeassistant/services", "/api/homeassistant/service",
        "/api/emby/sessions", "/api/emby/items", "/api/emby/control", "/api/voip/call",
        "/api/family/tasks", "/api/memory", "/api/memory/search", "/api/memory/{memory_id}",
        "/api/location/consent", "/api/location", "/api/family/locations",
        "/api/voice/stt", "/api/voice/tts", "/api/voice/enroll", "/api/voice/verify",
        "/api/voice/enrollment", "/api/voice/wake", "/media-library", "/api/media/{media_id}",
        "/releases", "/api/distrokid/releases/{release_id}/package",
        "/api/actions/proposals", "/api/actions/proposals/{proposal_id}",
        "/api/actions/proposals/{proposal_id}/execute", "/api/audit",
    }
    actual = {route.path for route in app.routes}
    assert expected <= actual, sorted(expected - actual)
    with TestClient(app) as client:
        bootstrap(client)
        data = client.get("/api/status").json()
        assert data["version"] == APP_VERSION == "2.5.0-family-brain-router"
        statuses = {item["provider"]: item["status"] for item in data["services"]}
        for provider in ("google_gmail", "google_calendar", "google_tasks", "google_youtube", "spotify", "tiktok"):
            assert statuses[provider] == "not_configured"
        assert statuses["distrokid"] == "ready"


def test_setup_validates_server_side_and_is_one_time() -> None:
    with TestClient(app) as client:
        invalid = client.post("/setup", data={"display_name": "A", "username": "x", "password": ADMIN_PASSWORD})
        assert invalid.status_code == 400
        bootstrap(client)
        second = client.post("/setup", data={"display_name": "Other", "username": "other", "password": ADMIN_PASSWORD})
        assert second.status_code == 409


def test_configuration_and_tokens_are_encrypted_and_blank_secret_fields_preserve_values() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        secret = "google-secret-must-not-appear-in-db"
        response = client.post(
            "/integrations/config",
            data={
                "csrf": csrf(client), "public_base_url": "https://athena.example.test",
                "google_client_id": "google-client-id", "google_client_secret": secret,
                "spotify_client_id": "spotify-id", "tiktok_client_key": "tt-key", "tiktok_client_secret": "tt-secret",
                "emby_base_url": "http://emby.local:8096", "emby_api_key": "emby-key", "emby_user_id": "emby-user",
                "asterisk_port": "5038",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = client.post(
            "/integrations/config",
            data={"csrf": csrf(client), "google_client_id": "google-client-id", "google_client_secret": "", "asterisk_port": "5038"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert get_app_config("google")["client_secret"] == secret
        assert public_base_url("http://internal:8000") == "https://athena.example.test"
        save_connection(admin_id(), "spotify", {"access_token": "private-token", "refresh_token": "private-refresh", "expires_in": 3600}, ["user-read-private"], "Account")
        raw = DB_PATH.read_bytes()
        for value in (secret, "private-token", "private-refresh"):
            assert value.encode() not in raw
        assert MASTER_KEY_PATH.stat().st_mode & 0o777 == 0o600


def test_persona_wizard_gate_forces_welcome_until_configured() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        # bootstrap() marks the wizard done so every other test is unaffected —
        # undo that here to exercise the actual first-login behavior.
        from app.persona import get_persona, set_persona

        set_persona(admin_id(), configured=False)

        response = client.get("/graph", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/welcome"

        # /welcome itself, static assets, and the API must stay reachable —
        # otherwise the wizard page couldn't even render or save.
        assert client.get("/welcome", follow_redirects=False).status_code == 200

        response = client.post(
            "/api/persona",
            headers={"X-CSRF-Token": csrf(client)},
            json={"assistant_name": "Ζωή", "persona_note": "", "voice_id": "", "avatar_url": ""},
        )
        assert response.status_code == 200
        assert get_persona(admin_id())["configured"] is True

        # Now that it's configured, normal navigation is unblocked again.
        assert client.get("/graph", follow_redirects=False).status_code == 200


def test_family_accounts_permissions_and_personal_connector_isolation() -> None:
    with TestClient(app) as admin:
        bootstrap(admin)
        child_id = add_child(admin)
        configure_minimal_apps(admin_id())
        save_connection(admin_id(), "google_gmail", {"access_token": "admin-token", "expires_in": 3600}, ["https://www.googleapis.com/auth/gmail.readonly"], "admin@example.test")
    with TestClient(app) as child:
        assert child.post("/login", data={"username": "child", "password": CHILD_PASSWORD}, follow_redirects=False).status_code == 303
        assert child.get("/admin/users").status_code == 403
        status = child.get("/api/status").json()
        assert status["permissions"]["gmail.read"] is False
        assert status["permissions"]["calendar.read"] is True
        providers = {item["provider"]: item for item in status["services"]}
        assert providers["google_gmail"]["status"] == "authorization_required"
        assert get_connection(child_id, "google_gmail") is None
        assert child.get("/oauth/google/gmail/start?mode=read", follow_redirects=False).status_code == 403
        assert child.get("/oauth/google/calendar/start?mode=read", follow_redirects=False).status_code == 302


def test_last_active_admin_cannot_be_disabled_or_demoted() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        response = client.post(
            f"/admin/users/{admin_id()}/edit",
            data={"display_name": "Administrator", "username": "admin", "role": "adult", "csrf": csrf(client)},
            follow_redirects=False,
        )
        assert response.status_code == 409


def test_login_rate_limit_is_enforced() -> None:
    with TestClient(app) as client:
        bootstrap(client)
    with TestClient(app) as attacker:
        for _ in range(8):
            assert attacker.post("/login", data={"username": "admin", "password": "wrong-password"}).status_code == 401
        assert attacker.post("/login", data={"username": "admin", "password": "wrong-password"}).status_code == 429


def test_confirmation_is_session_action_payload_bound_and_single_use() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        raw = client.cookies.get(SESSION_COOKIE)
        session_hash = token_hash(raw)
        payload = {"to": "person@example.test", "subject": "Subject", "body": "Body", "cc": "", "bcc": ""}
        token = issue_confirmation(admin_id(), session_hash, "gmail.send", payload)
        assert not consume_confirmation(token, admin_id(), session_hash, "gmail.send", {**payload, "body": "Changed"})
        assert not consume_confirmation(token, admin_id(), session_hash, "youtube.upload", payload)
        assert consume_confirmation(token, admin_id(), session_hash, "gmail.send", payload)
        assert not consume_confirmation(token, admin_id(), session_hash, "gmail.send", payload)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/gmail/send", {"to": "person@example.test", "subject": "S", "body": "B", "cc": "", "bcc": ""}),
        ("post", "/api/calendar/events", {"summary": "S", "start": {"dateTime": "2026-08-06T10:00:00Z"}, "end": {"dateTime": "2026-08-06T11:00:00Z"}}),
        ("post", "/api/google-tasks", {"title": "Task", "notes": "", "due": None}),
        ("post", "/api/youtube/upload", {"media_path": "users/x/video.mp4", "title": "Video"}),
        ("post", "/api/youtube/comments/parent/reply", {"text": "Reply"}),
        ("post", "/api/spotify/playback", {"action": "pause"}),
        ("post", "/api/spotify/playlists", {"name": "Playlist", "description": "", "public": False}),
        ("put", "/api/spotify/saved-tracks", {"ids": ["0" * 22]}),
        ("post", "/api/tiktok/publish", {"media_path": "users/x/video.mp4", "mode": "draft"}),
        ("post", "/api/homeassistant/service", {"domain": "light", "service": "turn_on", "service_data": {}}),
        ("post", "/api/emby/control", {"session_id": "abc", "command": "Pause"}),
        ("post", "/api/voip/call", {"channel": "PJSIP/100", "extension": "101"}),
        ("delete", "/api/memory/memory-id", None),
        ("delete", "/api/media/media-id", None),
        ("delete", "/api/voice/enrollment", None),
    ],
)
def test_state_changing_endpoints_require_confirmation_before_provider_access(method: str, path: str, payload: dict) -> None:
    with TestClient(app) as client:
        bootstrap(client)
        kwargs = {"headers": {"X-CSRF-Token": csrf(client)}}
        if payload is not None:
            kwargs["json"] = payload
        response = client.request(method, path, **kwargs)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["detail"]["status"] == "confirmation_required"


def test_google_and_spotify_oauth_use_state_and_pkce_with_encrypted_verifier() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        configure_minimal_apps(admin_id())
        google = client.get("/oauth/google/gmail/start?mode=read", follow_redirects=False)
        spotify = client.get("/oauth/spotify/start", follow_redirects=False)
        assert google.status_code == spotify.status_code == 302
        for response in (google, spotify):
            query = parse_qs(urlparse(response.headers["location"]).query)
            assert len(query["state"][0]) >= 64
            assert query["code_challenge_method"] == ["S256"]
            assert len(query["code_challenge"][0]) >= 43
        with connect() as conn:
            rows = conn.execute("SELECT provider,verifier_enc,state_hash FROM oauth_states ORDER BY created_at").fetchall()
        assert {row["provider"] for row in rows} == {"google_gmail", "spotify"}
        raw = DB_PATH.read_bytes()
        for row in rows:
            verifier = decrypt_json(row["verifier_enc"])["verifier"]
            assert verifier.encode() not in raw
        assert all(len(row["state_hash"]) == 64 for row in rows)


def test_oauth_scope_enforcement_rejects_connected_but_under_scoped_account() -> None:
    init_db()
    user_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, "u1", "User", password_hash(ADMIN_PASSWORD), "adult", utcnow(), utcnow()))
    save_connection(user_id, "google_gmail", {"access_token": "token", "expires_in": 3600}, ["openid"], "u@example.test")
    with pytest.raises(IntegrationError) as error:
        require_connection_scopes(user_id, "google_gmail", ["https://www.googleapis.com/auth/gmail.readonly"])
    assert error.value.status == "permission_denied"
    assert "gmail.readonly" in error.value.details["missing_scopes"][0]


def test_google_revoke_removes_all_google_connections_but_not_other_providers(monkeypatch) -> None:
    init_db()
    user_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, "u1", "User", password_hash(ADMIN_PASSWORD), "adult", utcnow(), utcnow()))
    for provider in ("google_gmail", "google_calendar"):
        save_connection(user_id, provider, {"access_token": "token", "refresh_token": "refresh", "expires_in": 3600}, ["openid"], "u@example.test")
    save_connection(user_id, "spotify", {"access_token": "spotify", "expires_in": 3600}, ["user-read-private"], "Spotify")

    class Response:
        status_code = 200
        text = ""
        def json(self): return {}

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(integrations.httpx, "AsyncClient", Client)
    result = asyncio.run(revoke_connection(user_id, "google_gmail"))
    assert result["provider_revoke"] == "revoked"
    assert set(result["affected_connections"]) == {"google_gmail", "google_calendar"}
    assert get_connection(user_id, "google_gmail") is None
    assert get_connection(user_id, "google_calendar") is None
    assert get_connection(user_id, "spotify") is not None


def test_private_media_library_is_user_isolated() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        child_id = add_child(client)
        response = client.post(
            "/media-library", data={"csrf": csrf(client)},
            files={"media": ("video.mp4", b"stored-private-media", "video/mp4")}, follow_redirects=False,
        )
        assert response.status_code == 303
        with connect() as conn:
            media_row = conn.execute("SELECT relative_path,owner_id FROM media_files").fetchone()
        assert safe_user_media_path(media_row["owner_id"], media_row["relative_path"]).is_file()
        with pytest.raises(IntegrationError) as error:
            safe_user_media_path(child_id, media_row["relative_path"])
        assert error.value.status == "permission_denied"


def test_memory_namespaces_are_isolated(monkeypatch) -> None:
    init_db()
    now = utcnow()
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
    with connect() as conn:
        for uid, name in ((u1, "u1"), (u2, "u2")):
            conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (uid, name, name, password_hash(ADMIN_PASSWORD), "adult", now, now))
    memory.add_memory(u1, "private", "secret one")
    memory.add_memory(u2, "private", "secret two")
    memory.add_memory(u1, "family_shared", "family secret")
    found = memory.search_memory(u1, "secret", namespaces=["private", "family_shared"])
    texts = {item["text"] for item in found}
    assert "secret one" in texts and "family secret" in texts
    assert "secret two" not in texts


def test_location_requires_explicit_consent_and_family_sharing() -> None:
    with TestClient(app) as admin:
        bootstrap(admin)
        add_child(admin)
        denied = admin.post("/api/location", headers={"X-CSRF-Token": csrf(admin)}, json={"latitude": 37.9, "longitude": 23.7})
        assert denied.status_code == 403
        assert admin.post("/api/location/consent", headers={"X-CSRF-Token": csrf(admin)}, json={"enabled": True, "share_with_family": True, "retention_hours": 24}).status_code == 200
        assert admin.post("/api/location", headers={"X-CSRF-Token": csrf(admin)}, json={"latitude": 37.9, "longitude": 23.7, "accuracy": 10, "source": "browser"}).status_code == 200
    with TestClient(app) as child:
        assert child.post("/login", data={"username": "child", "password": CHILD_PASSWORD}, follow_redirects=False).status_code == 303
        data = child.get("/api/family/locations").json()
        assert len(data["items"]) == 1
        assert data["items"][0]["display_name"] == "Administrator"


def test_homeassistant_allowlist_blocks_unapproved_domains_before_confirmation() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        set_app_config("homeassistant", {"base_url": "http://ha.local:8123", "token": "token", "allowed_domains": ["light"]}, admin_id())
        blocked = client.post("/api/homeassistant/service", headers={"X-CSRF-Token": csrf(client)}, json={"domain": "lock", "service": "unlock", "service_data": {}})
        assert blocked.status_code == 403
        allowed = client.post("/api/homeassistant/service", headers={"X-CSRF-Token": csrf(client)}, json={"domain": "light", "service": "turn_on", "service_data": {}})
        assert allowed.status_code == 409


def test_asterisk_ami_rejects_header_injection() -> None:
    with pytest.raises(IntegrationError) as error:
        _ami_packet({"Action": "Originate", "Channel": "PJSIP/100\r\nAction: Logoff"})
    assert error.value.status == "invalid_request"


def test_distrokid_workspace_generates_valid_multitrack_package() -> None:
    with TestClient(app) as client:
        bootstrap(client)
        response = client.post(
            "/releases",
            data={
                "artist": "Artist", "title": "Album", "metadata_json": json.dumps({"genre": "Pop", "tracks": [{"title": "One"}, {"title": "Two"}]}),
                "rights_confirmed": "true", "csrf": csrf(client),
            },
            files=[
                ("audio", ("one.wav", valid_wav(), "audio/wav")),
                ("audio", ("two.wav", valid_wav(), "audio/wav")),
                ("artwork", ("cover.png", valid_png(), "image/png")),
            ],
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            release = conn.execute("SELECT * FROM release_workspaces").fetchone()
            tracks = conn.execute("SELECT * FROM release_tracks ORDER BY track_number").fetchall()
        assert release["status"] == "package_ready"
        assert [row["title"] for row in tracks] == ["One", "Two"]
        package = Path(release["package_path"])
        assert package.is_file()
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            assert "manifest.json" in names and "artwork.png" in names
            assert len([name for name in names if name.startswith("audio/")]) == 2
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["submission_mode"] == "manual_official_web"
            assert len(manifest["tracks"]) == 2


def test_llm_proposal_is_not_executed_without_separate_confirmation(monkeypatch) -> None:
    with TestClient(app) as client:
        bootstrap(client)
        user = {"id": admin_id(), "role": "admin", "display_name": "Administrator"}
        result = asyncio.run(orchestrator.execute_tool(user, "propose_confirmed_action", {
            "action": "gmail.send", "payload": {"to": "x@example.test", "subject": "S", "body": "B", "cc": "", "bcc": ""}, "summary": "Send email",
        }))
        assert result["status"] == "confirmation_required"
        proposal_id = result["proposal_id"]
        pending = client.get(f"/api/actions/proposals/{proposal_id}").json()
        assert pending["status"] == "pending"
        response = client.post(f"/api/actions/proposals/{proposal_id}/execute", headers={"X-CSRF-Token": csrf(client)})
        assert response.status_code == 409
        with connect() as conn:
            row = conn.execute("SELECT status FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
        assert row["status"] == "pending"


def test_action_proposal_executes_once_after_exact_confirmation(monkeypatch) -> None:
    calls = []
    async def fake_send(user_id: str, **payload):
        calls.append((user_id, payload))
        return {"id": "message-1"}
    monkeypatch.setattr("app.main.gmail_send", fake_send)
    with TestClient(app) as client:
        bootstrap(client)
        user = {"id": admin_id(), "role": "admin", "display_name": "Administrator"}
        proposal = asyncio.run(orchestrator.execute_tool(user, "propose_confirmed_action", {
            "action": "gmail.send", "payload": {"to": "x@example.test", "subject": "S", "body": "B", "cc": "", "bcc": ""}, "summary": "Send email",
        }))
        view = client.get(f"/api/actions/proposals/{proposal['proposal_id']}").json()
        confirmation = client.post("/api/confirmations", headers={"X-CSRF-Token": csrf(client)}, json={
            "action": view["confirmation_action"], "payload": view["confirmation_payload"], "password": ADMIN_PASSWORD,
        })
        assert confirmation.status_code == 200
        executed = client.post(
            f"/api/actions/proposals/{proposal['proposal_id']}/execute",
            headers={"X-CSRF-Token": csrf(client), "X-ATHENA-Confirmation": confirmation.json()["token"]},
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "executed"
        assert len(calls) == 1
        assert client.post(f"/api/actions/proposals/{proposal['proposal_id']}/execute", headers={"X-CSRF-Token": csrf(client)}).status_code == 404



def test_cliproxy_bootstrap_creates_secure_persistent_runtime_without_provider_credentials() -> None:
    data = cliproxy.bootstrap()
    assert len(data["api_key"]) >= 32
    assert len(data["management_key"]) >= 32
    assert cliproxy.CLIPROXY_SECRETS_PATH.stat().st_mode & 0o777 == 0o600
    assert cliproxy.CLIPROXY_CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    config = cliproxy.CLIPROXY_CONFIG_PATH.read_text(encoding="utf-8")
    assert 'port: 8317' in config
    assert 'auth-dir: "/config/cliproxy/auths"' in config
    assert 'disable-control-panel: false' in config
    assert data["api_key"] in config
    assert data["management_key"] in config
    lowered = config.lower()
    for provider_secret in ("openai-api-key", "anthropic-api-key", "gemini-api-key", "grok-api-key"):
        assert provider_secret not in lowered


def test_cliproxy_discovers_four_brain_families_and_routes_automatically(monkeypatch) -> None:
    init_db()
    cliproxy.bootstrap()

    class Response:
        status_code = 200
        text = ""
        def json(self):
            return {"data": [{"id": "claude-model"}, {"id": "gemini-model"}, {"id": "gpt-model"}, {"id": "grok-model"}]}

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, headers=None):
            assert url.endswith("/v1/models")
            assert headers["Authorization"].startswith("Bearer ")
            return Response()

    monkeypatch.setattr(cliproxy.httpx, "AsyncClient", Client)
    catalog = asyncio.run(cliproxy.model_catalog())
    assert catalog["status"] == "connected"
    assert catalog["selection"] == "athena_automatic"
    assert catalog["connected_families"] == ["claude", "codex_openai", "gemini", "grok"]
    assert catalog["missing_families"] == []
    route = asyncio.run(cliproxy.automatic_route("Διόρθωσε αυτό το Python Docker workflow και το API"))
    assert route["selection"] == "athena_automatic"
    assert route["primary_family"] == "codex_openai"
    assert route["primary_model"] == "gpt-model"
    assert set(route["fallback_models"]) == {"claude-model", "gemini-model", "grok-model"}


def test_llm_admin_routes_and_management_key_require_admin_password(monkeypatch) -> None:
    async def fake_catalog():
        return {
            "status": "connected",
            "selection": "athena_automatic",
            "models": ["claude-model", "gemini-model", "gpt-model", "grok-model"],
            "families": {
                "claude": ["claude-model"],
                "codex_openai": ["gpt-model"],
                "gemini": ["gemini-model"],
                "grok": ["grok-model"],
            },
            "connected_families": ["claude", "codex_openai", "gemini", "grok"],
            "missing_families": [],
        }

    monkeypatch.setattr("app.main.model_catalog", fake_catalog)
    with TestClient(app) as client:
        bootstrap(client)
        page = client.get("/admin/llm")
        assert page.status_code == 200
        assert "router-for-me/CLIProxyAPI" in page.text
        assert "Claude" in page.text and "Gemini" in page.text and "Grok" in page.text and "Codex/OpenAI" in page.text
        assert "Δεν υπάρχει και δεν επιτρέπεται χειροκίνητη επιλογή μοντέλου" in page.text
        assert "/admin/llm/default" not in page.text
        assert "<select" not in page.text
        wrong = client.post("/admin/llm/credentials", data={"csrf": csrf(client), "password": "wrong-password"})
        assert wrong.status_code == 401
        correct = client.post("/admin/llm/credentials", data={"csrf": csrf(client), "password": ADMIN_PASSWORD})
        assert correct.status_code == 200
        assert cliproxy.secrets_data()["management_key"] in correct.text
        models = client.get("/api/llm/models")
        assert models.status_code == 200
        assert models.json()["models"] == ["claude-model", "gemini-model", "gpt-model", "grok-model"]
        assert models.json()["selection"] == "athena_automatic"


def test_orchestrator_selects_brain_internally_and_preserves_tool_calls(monkeypatch) -> None:
    seen = {}

    async def fake_route(question):
        assert question == "hello"
        return {
            "selection": "athena_automatic",
            "primary_family": "grok",
            "primary_model": "grok-model",
            "fallback_models": ["claude-model", "gpt-model", "gemini-model"],
            "reason": "realtime_or_social_task",
            "brain_status": "connected",
        }

    async def fake_chat_stream(payload, timeout=180.0):
        seen.update(payload)
        yield {"choices": [{"delta": {"content": "router answer"}}]}

    monkeypatch.setattr(orchestrator, "automatic_route", fake_route)
    monkeypatch.setattr(orchestrator, "chat_completions_stream", fake_chat_stream)
    init_db()
    user_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, "routeruser", "Router User", password_hash(ADMIN_PASSWORD), "admin", now, now))
    result = asyncio.run(orchestrator.ask({"id": user_id, "role": "admin", "display_name": "Router User"}, "hello"))
    assert result["status"] == "ready"
    assert result["brain"]["selection"] == "athena_automatic"
    assert result["brain"]["route_family"] == "grok"
    assert "model" not in result
    assert "available_models" not in result
    assert seen["model"] == "grok-model"
    assert seen["tools"]


def test_orchestrator_automatically_falls_back_to_another_brain_family(monkeypatch) -> None:
    attempts = []

    async def fake_route(question):
        return {
            "selection": "athena_automatic",
            "primary_family": "codex_openai",
            "primary_model": "gpt-model",
            "fallback_models": ["claude-model", "gemini-model", "grok-model"],
            "reason": "software_and_technical_task",
            "brain_status": "connected",
        }

    async def fake_chat_stream(payload, timeout=180.0):
        attempts.append(payload["model"])
        if payload["model"] == "gpt-model":
            raise cliproxy.CLIProxyError("provider_unavailable", "provider unavailable")
        yield {"choices": [{"delta": {"content": "fallback answer"}}]}

    monkeypatch.setattr(orchestrator, "automatic_route", fake_route)
    monkeypatch.setattr(orchestrator, "chat_completions_stream", fake_chat_stream)
    init_db()
    user_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, "fallbackuser", "Fallback User", password_hash(ADMIN_PASSWORD), "admin", now, now))
    result = asyncio.run(orchestrator.ask({"id": user_id, "role": "admin", "display_name": "Fallback User"}, "Διόρθωσε το Docker code"))
    assert result["status"] == "ready"
    assert attempts[:2] == ["gpt-model", "claude-model"]
    assert result["brain"]["route_family"] == "claude"
    assert result["brain"]["failover_count"] == 1


def test_ask_api_rejects_manual_model_selection(monkeypatch) -> None:
    async def fake_ask(user, question, on_delta=None, voice=False):
        return {"status": "ready", "answer": question, "brain": {"selection": "athena_automatic"}}
    monkeypatch.setattr("app.main.ask", fake_ask)
    with TestClient(app) as client:
        bootstrap(client)
        headers = {"X-CSRF-Token": csrf(client)}
        denied = client.post("/api/ask", headers=headers, json={"question": "hello", "model": "grok-model"})
        assert denied.status_code == 422
        accepted = client.post("/api/ask", headers=headers, json={"question": "hello"})
        assert accepted.status_code == 200
        events = [json.loads(line) for line in accepted.text.strip().split("\n")]
        done = next(e for e in events if e["event"] == "done")
        assert done["result"]["brain"]["selection"] == "athena_automatic"


def test_no_fabricated_success_personal_secrets_or_multiple_image_contract() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").glob("*.py"))
    forbidden = [
        r"fake[_ -]?success", r"simulated[_ -]?success", r"return\s+\{[^\n]*['\"]success['\"]\s*:\s*True",
        r"dummy[_-]?(?:key|token|secret)", r"changeme", r"sk-[A-Za-z0-9]{20,}", r"@example\.(?:com|gr)",
    ]
    for pattern in forbidden:
        assert re.search(pattern, source, flags=re.IGNORECASE) is None, pattern
    manifest = json.loads((ROOT / "FEATURES.json").read_text(encoding="utf-8"))
    assert manifest["version"] == APP_VERSION
    assert manifest["deployment"]["own_images"] == 1
    assert manifest["deployment"]["containers"] == 1
    assert len(manifest["features"]) >= 30
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert "eceasy/cli-proxy-api" in dockerfile
    assert "FROM ${CLIPROXY_IMAGE} AS cliproxy" in dockerfile
    assert "COPY --from=cliproxy" in dockerfile
    assert "/usr/local/bin/CLIProxyAPI" in entrypoint
    assert "uvicorn app.main:app" in entrypoint
    assert "HEALTHCHECK" in dockerfile


def test_application_source_compiles() -> None:
    import compileall
    assert compileall.compile_dir(ROOT / "app", quiet=1)


def test_dashboard_common_javascript_keeps_api_helper_syntactically_reachable() -> None:
    """Regression test for the browser error: `api is not defined`."""
    with TestClient(app) as client:
        bootstrap(client)
        response = client.get("/")
        assert response.status_code == 200
        page = response.text
        assert "async function api(" in page
        assert "async function askAthena()" in page
        # The confirmation prompt must contain the two-character JS escape, not
        # a literal newline inside a single-quoted JavaScript string.
        assert "+action+'\\nΠληκτρολόγησε τον κωδικό σου:'" in page
        assert "+action+'\nΠληκτρολόγησε τον κωδικό σου:'" not in page
