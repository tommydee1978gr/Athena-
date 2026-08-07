"""MCP client — lets ATHENA use tools from any external MCP server without a
core code change. An admin registers a server (stdio command, or an http
URL); every enabled server's tools show up in orchestrator.available_tools()
next to the built-in ones, namespaced mcp_<server>_<tool> so they can never
collide with a built-in name.

Connections are short-lived and per-call (open, do one thing, close) rather
than long-lived background subprocesses. That costs a bit of latency on every
call but means a wedged or crashed MCP server can never take ATHENA itself
down — there is no persistent state to get stuck.

Tool discovery is cached in memory with a TTL and refreshed by the scheduler,
because discovery is async I/O and orchestrator.available_tools() is called
synchronously from inside the request path.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .db import connect, utcnow
from .integrations import IntegrationError, _json_request, _pkce_pair, public_base_url, store_oauth_state
from .security import decrypt_json, encrypt_json, token_hash

logger = logging.getLogger("athena.mcp")

_CACHE_TTL_SECONDS = 300
_tool_cache: dict[str, dict[str, Any]] = {}  # qualified_name -> {server_id, server_name, tool_name, description, input_schema}
_cache_refreshed_at: float = 0.0


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text.strip())[:60] or "server"


def add_server(name: str, transport: str, config: dict[str, Any], created_by: str, requires_confirmation: bool = True) -> dict[str, Any]:
    if transport not in {"stdio", "http"}:
        raise ValueError("transport must be stdio or http")
    if transport == "stdio" and not config.get("command"):
        raise ValueError("stdio transport requires config.command (a list, e.g. ['python', '-m', 'my_server'])")
    if transport == "http" and not config.get("url"):
        raise ValueError("http transport requires config.url")
    server_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO mcp_servers(id,name,transport,config_enc,enabled,requires_confirmation,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (server_id, name.strip(), transport, encrypt_json(config), 1, int(requires_confirmation), created_by, now, now),
        )
    _invalidate_cache()
    return {"id": server_id, "name": name, "transport": transport, "requires_confirmation": requires_confirmation}


def list_servers() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id,name,transport,config_enc,enabled,requires_confirmation,created_at FROM mcp_servers ORDER BY created_at DESC").fetchall()
    items = []
    for row in rows:
        config = decrypt_json(row["config_enc"])
        oauth_cfg = config.get("oauth")
        oauth_status = "not_applicable"
        if oauth_cfg:
            oauth_status = "connected" if oauth_cfg.get("token", {}).get("access_token") else "registered"
        items.append({
            "id": row["id"], "name": row["name"], "transport": row["transport"],
            "enabled": row["enabled"], "requires_confirmation": bool(row["requires_confirmation"]),
            "created_at": row["created_at"], "oauth_status": oauth_status,
        })
    return items


def remove_server(server_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
    _invalidate_cache()
    return cur.rowcount > 0


def set_enabled(server_id: str, enabled: bool) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE mcp_servers SET enabled=?, updated_at=? WHERE id=?", (int(enabled), utcnow(), server_id))
    _invalidate_cache()
    return cur.rowcount > 0


def _get_server_config(server_id: str) -> tuple[str, str, dict[str, Any]] | None:
    with connect() as conn:
        row = conn.execute("SELECT name,transport,config_enc FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        return None
    return row["name"], row["transport"], decrypt_json(row["config_enc"])


@asynccontextmanager
async def _session_for(transport: str, config: dict[str, Any], server_id: str | None = None):
    if transport == "stdio":
        params = StdioServerParameters(command=config["command"][0], args=list(config["command"][1:]) + list(config.get("args", [])), env=config.get("env"))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        # Most hosted MCP servers (Higgsfield, OpenArt, ...) need auth on every
        # request — streamable_http_client takes a pre-configured httpx client
        # rather than a headers dict directly. Static header auth (config
        # ["headers"]) and OAuth bearer tokens (config["oauth"], see
        # start_oauth_flow/finish_oauth_flow below) both funnel in here.
        headers = dict(config.get("headers") or {})
        if config.get("oauth") and server_id:
            token = await _mcp_access_token(server_id, config)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        http_client = httpx.AsyncClient(headers=headers, timeout=60) if headers else None
        try:
            async with streamable_http_client(config["url"], http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        finally:
            if http_client is not None:
                await http_client.aclose()


def _invalidate_cache() -> None:
    global _cache_refreshed_at
    _cache_refreshed_at = 0.0
    _tool_cache.clear()


async def refresh_tool_cache(force: bool = False) -> int:
    global _cache_refreshed_at
    if not force and _tool_cache and (time.monotonic() - _cache_refreshed_at) < _CACHE_TTL_SECONDS:
        return len(_tool_cache)
    with connect() as conn:
        rows = conn.execute("SELECT id,name,transport,config_enc,requires_confirmation FROM mcp_servers WHERE enabled=1").fetchall()
    fresh: dict[str, dict[str, Any]] = {}
    for row in rows:
        server_id, name, transport = row["id"], row["name"], row["transport"]
        config = decrypt_json(row["config_enc"])
        try:
            async with _session_for(transport, config, server_id) as session:
                result = await session.list_tools()
        except Exception as exc:
            logger.warning("MCP server %r unreachable during tool discovery: %s", name, exc)
            continue
        for tool in result.tools:
            qualified = f"mcp_{_slug(name)}_{_slug(tool.name)}"
            fresh[qualified] = {
                "server_id": server_id,
                "server_name": name,
                "tool_name": tool.name,
                "description": (tool.description or "")[:1000],
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                # Defaults to True — an MCP server we know nothing about is
                # treated as capable of spending money until an admin
                # explicitly marks it safe to auto-run.
                "requires_confirmation": bool(row["requires_confirmation"]),
            }
    _tool_cache.clear()
    _tool_cache.update(fresh)
    _cache_refreshed_at = time.monotonic()
    logger.info("MCP tool cache refreshed: %d tool(s) across %d server(s)", len(_tool_cache), len(rows))
    return len(_tool_cache)


def cached_tools_as_functions() -> list[dict[str, Any]]:
    """Synchronous — reads whatever is already in the cache. Call
    refresh_tool_cache() from the scheduler to keep it warm; this function
    never blocks on network/subprocess I/O itself."""
    tools = []
    for qualified, entry in _tool_cache.items():
        note = " Requires human confirmation before it actually runs — call it, then tell the user it's pending approval." if entry["requires_confirmation"] else ""
        tools.append({
            "type": "function",
            "function": {
                "name": qualified,
                "description": f"[MCP: {entry['server_name']}] {entry['description']}{note}",
                "parameters": entry["input_schema"],
            },
        })
    return tools


def tool_requires_confirmation(qualified_name: str) -> bool:
    entry = _tool_cache.get(qualified_name)
    return bool(entry and entry["requires_confirmation"])


async def call_cached_tool(qualified_name: str, arguments: dict[str, Any]) -> Any:
    entry = _tool_cache.get(qualified_name)
    if not entry:
        return {"status": "error", "error": f"Unknown or stale MCP tool {qualified_name!r} — try again after the next tool refresh"}
    resolved = _get_server_config(entry["server_id"])
    if not resolved:
        return {"status": "error", "error": "MCP server was removed"}
    name, transport, config = resolved
    try:
        async with _session_for(transport, config, entry["server_id"]) as session:
            result = await session.call_tool(entry["tool_name"], arguments)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    return {"text": "\n".join(texts)} if texts else {"status": "ok", "content_blocks": len(result.content)}


# --- OAuth 2.1 for hosted MCP servers (Higgsfield, OpenArt, ...) ---------
#
# Some MCP servers require the caller to be an authorized user rather than
# just holding a static API key (Higgsfield/OpenArt both do — confirmed live
# by probing them: a raw `initialize` call returns 401 with a `WWW-Authenticate:
# Bearer resource_metadata="..."` header). That's the standard MCP auth spec:
# OAuth 2.1 + PKCE + Dynamic Client Registration (RFC 7591) + Protected
# Resource/Authorization Server metadata discovery (RFC 9728 / RFC 8414).
#
# ATHENA already has this exact shape of code for Google/Spotify/TikTok/
# Instagram in integrations.py, but that machinery is keyed on a per-user
# `connections` row (one Google token per family member). MCP servers are
# admin-configured and shared, so the token lives on the mcp_servers row
# itself (inside config_enc, under an "oauth" key) instead — no schema
# change needed. The CSRF-protecting state/PKCE-verifier dance during the
# redirect still reuses the existing `oauth_states` table via
# store_oauth_state(), with provider set to f"mcp:{server_id}".

def _update_server_config(server_id: str, config: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("UPDATE mcp_servers SET config_enc=?, updated_at=? WHERE id=?", (encrypt_json(config), utcnow(), server_id))


async def _discover_oauth_metadata(server_url: str) -> dict[str, Any]:
    """Walk RFC 9728 (protected resource) -> RFC 8414/OIDC (authorization
    server) discovery starting from the MCP server's own URL, and return the
    bits we need: authorization_endpoint, token_endpoint,
    registration_endpoint, scopes, resource."""
    parts = urlsplit(server_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    resource_metadata_url = f"{origin}/.well-known/oauth-protected-resource{parts.path}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(resource_metadata_url)
        if resp.status_code >= 400:
            # Fall back to the spec's other discovery path: probe the MCP
            # endpoint itself and read the resource_metadata URL out of the
            # 401's WWW-Authenticate header, per RFC 9728 §5.1.
            probe = await client.post(server_url, headers={"Accept": "application/json, text/event-stream"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "ATHENA", "version": "1.0"}},
            })
            challenge = probe.headers.get("www-authenticate", "")
            match = re.search(r'resource_metadata="([^"]+)"', challenge)
            if not match:
                raise IntegrationError("not_configured", "This MCP server does not advertise OAuth discovery metadata", http_status=409)
            resp = await client.get(match.group(1))
            if resp.status_code >= 400:
                raise IntegrationError("provider_unavailable", "OAuth protected-resource metadata request failed", http_status=502)
        resource_meta = resp.json()
        authorization_servers = resource_meta.get("authorization_servers") or []
        if not authorization_servers:
            raise IntegrationError("not_configured", "OAuth protected-resource metadata lists no authorization server", http_status=409)
        as_meta: dict[str, Any] | None = None
        for as_url in authorization_servers:
            for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                candidate = await client.get(f"{as_url.rstrip('/')}{suffix}")
                if candidate.status_code < 400:
                    data = candidate.json()
                    if data.get("authorization_endpoint") and data.get("registration_endpoint"):
                        as_meta = data
                        break
            if as_meta:
                break
        if not as_meta:
            raise IntegrationError("not_configured", "No authorization server offering a browser redirect + dynamic registration flow was found", http_status=409)
    return {
        "authorization_endpoint": as_meta["authorization_endpoint"],
        "token_endpoint": as_meta["token_endpoint"],
        "registration_endpoint": as_meta["registration_endpoint"],
        "scopes": resource_meta.get("scopes_supported") or as_meta.get("scopes_supported") or [],
        "resource": resource_meta.get("resource", server_url),
        "token_endpoint_auth_methods_supported": as_meta.get("token_endpoint_auth_methods_supported", ["none"]),
    }


async def _register_client(meta: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
    """RFC 7591 Dynamic Client Registration — ATHENA registers itself as a
    public (no client secret) PKCE client with whatever server we're
    connecting to. Falls back to client_secret_post if the server refuses a
    public client."""
    auth_method = "none" if "none" in meta.get("token_endpoint_auth_methods_supported", ["none"]) else "client_secret_post"
    body = {
        "client_name": "ATHENA",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    data = await _json_request("POST", meta["registration_endpoint"], json=body)
    if not data.get("client_id"):
        raise IntegrationError("provider_unavailable", "Dynamic client registration did not return a client_id", http_status=502, details=data)
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret")}


async def start_oauth_flow(server_id: str, user_id: str, request_base: str) -> str:
    """Called from the /mcp/{id}/oauth/start admin route. Discovers the
    server's OAuth metadata (once — cached on the server row after that),
    registers ATHENA as a client if needed, and returns the authorization
    URL to redirect the admin's browser to."""
    resolved = _get_server_config(server_id)
    if not resolved:
        raise IntegrationError("not_found", "MCP server not found", http_status=404)
    name, transport, config = resolved
    if transport != "http" or not config.get("url"):
        raise IntegrationError("invalid_request", "OAuth only applies to http-transport MCP servers", http_status=400)
    redirect_uri = f"{public_base_url(request_base)}/mcp/oauth/callback"
    oauth_cfg = config.get("oauth") or {}
    if not oauth_cfg.get("client_id") or not oauth_cfg.get("authorization_endpoint"):
        meta = await _discover_oauth_metadata(config["url"])
        client = await _register_client(meta, redirect_uri)
        oauth_cfg = {
            "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"],
            "resource": meta.get("resource", config["url"]),
            "scopes": meta.get("scopes", []),
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret"),
        }
        config["oauth"] = oauth_cfg
        _update_server_config(server_id, config)
    verifier, challenge = _pkce_pair()
    state = uuid.uuid4().hex + uuid.uuid4().hex
    store_oauth_state(user_id, f"mcp:{server_id}", redirect_uri, oauth_cfg.get("scopes", []), state, verifier)
    params = {
        "client_id": oauth_cfg["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(oauth_cfg.get("scopes", [])), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if oauth_cfg.get("resource"):
        params["resource"] = oauth_cfg["resource"]
    return f"{oauth_cfg['authorization_endpoint']}?{urlencode(params)}"


async def finish_oauth_flow(code: str, state: str) -> tuple[str, str, str]:
    """Called from the /mcp/oauth/callback route once the admin approves
    access on the server's own consent screen. Exchanges the code for a
    token and stores it on the mcp_servers row. Returns (user_id, server_id,
    name) — user_id is whoever started the flow, for audit attribution."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state_hash=? AND provider LIKE 'mcp:%' AND expires_at>?",
            (token_hash(state), utcnow()),
        ).fetchone()
        if not row:
            raise IntegrationError("permission_denied", "OAuth state is invalid or expired", http_status=400)
        conn.execute("DELETE FROM oauth_states WHERE state_hash=?", (row["state_hash"],))
    verifier = decrypt_json(row["verifier_enc"]).get("verifier", "") if row["verifier_enc"] else ""
    server_id = row["provider"].split(":", 1)[1]
    resolved = _get_server_config(server_id)
    if not resolved:
        raise IntegrationError("not_found", "MCP server was removed before OAuth completed", http_status=404)
    name, transport, config = resolved
    oauth_cfg = config.get("oauth") or {}
    if not oauth_cfg.get("token_endpoint"):
        raise IntegrationError("invalid_request", "OAuth was not started for this server", http_status=400)
    body = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": row["redirect_uri"],
        "client_id": oauth_cfg.get("client_id", ""), "code_verifier": verifier,
    }
    if oauth_cfg.get("client_secret"):
        body["client_secret"] = oauth_cfg["client_secret"]
    if oauth_cfg.get("resource"):
        body["resource"] = oauth_cfg["resource"]
    token = await _json_request("POST", oauth_cfg["token_endpoint"], data=body)
    oauth_cfg["token"] = token
    if token.get("expires_in"):
        oauth_cfg["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=int(token["expires_in"]) - 30)).isoformat()
    else:
        oauth_cfg.pop("expires_at", None)
    config["oauth"] = oauth_cfg
    _update_server_config(server_id, config)
    _invalidate_cache()
    return row["user_id"], server_id, name


async def _mcp_access_token(server_id: str, config: dict[str, Any]) -> str | None:
    """Returns a valid bearer token for an OAuth-connected MCP server,
    refreshing it first if it's expired. Returns None if OAuth was
    registered but the admin hasn't completed the authorize step yet."""
    oauth_cfg = config.get("oauth") or {}
    token = oauth_cfg.get("token") or {}
    if not token.get("access_token"):
        return None
    expires_at = oauth_cfg.get("expires_at")
    if expires_at and expires_at <= datetime.now(timezone.utc).isoformat():
        if not token.get("refresh_token"):
            return token.get("access_token")  # expired with nothing to refresh — let the call fail with a clear 401 from the server
        body = {
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            "client_id": oauth_cfg.get("client_id", ""),
        }
        if oauth_cfg.get("client_secret"):
            body["client_secret"] = oauth_cfg["client_secret"]
        try:
            refreshed = await _json_request("POST", oauth_cfg["token_endpoint"], data=body)
        except IntegrationError as exc:
            logger.warning("MCP OAuth refresh failed for server %s: %s", server_id, exc)
            return token.get("access_token")
        refreshed.setdefault("refresh_token", token["refresh_token"])
        oauth_cfg["token"] = refreshed
        if refreshed.get("expires_in"):
            oauth_cfg["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=int(refreshed["expires_in"]) - 30)).isoformat()
        config["oauth"] = oauth_cfg
        _update_server_config(server_id, config)
        return refreshed.get("access_token")
    return token.get("access_token")
