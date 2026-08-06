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
from typing import Any

import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .db import connect, utcnow
from .security import decrypt_json, encrypt_json

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
        rows = conn.execute("SELECT id,name,transport,enabled,requires_confirmation,created_at FROM mcp_servers ORDER BY created_at DESC").fetchall()
    return [{**dict(row), "requires_confirmation": bool(row["requires_confirmation"])} for row in rows]


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
async def _session_for(transport: str, config: dict[str, Any]):
    if transport == "stdio":
        params = StdioServerParameters(command=config["command"][0], args=list(config["command"][1:]) + list(config.get("args", [])), env=config.get("env"))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        # Most hosted MCP servers (Higgsfield, OpenArt, ...) need an API key
        # on every request — streamable_http_client takes a pre-configured
        # httpx client rather than a headers dict directly.
        headers = config.get("headers") or {}
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
            async with _session_for(transport, config) as session:
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
        async with _session_for(transport, config) as session:
            result = await session.call_tool(entry["tool_name"], arguments)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    return {"text": "\n".join(texts)} if texts else {"status": "ok", "content_blocks": len(result.content)}
