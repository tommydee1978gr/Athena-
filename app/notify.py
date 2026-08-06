"""Outbound notification fan-out — the first channel that lets ATHENA speak
without being asked first. ntfy.sh (or a self-hosted ntfy) needs no account:
a channel is just a topic name plus an optional server URL. Pick a topic that
is hard to guess if using the public ntfy.sh, since anyone who knows the topic
can subscribe to it.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from .db import connect, utcnow
from .security import decrypt_json, encrypt_json

DEFAULT_NTFY_SERVER = "https://ntfy.sh"


def add_channel(owner_id: str, topic: str, server: str = DEFAULT_NTFY_SERVER) -> dict[str, Any]:
    channel_id = str(uuid.uuid4())
    now = utcnow()
    config = {"topic": topic.strip(), "server": server.strip().rstrip("/") or DEFAULT_NTFY_SERVER}
    with connect() as conn:
        conn.execute(
            "INSERT INTO notification_channels(id,owner_id,kind,config_enc,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (channel_id, owner_id, "ntfy", encrypt_json(config), 1, now, now),
        )
    return {"id": channel_id, "kind": "ntfy", "topic": config["topic"], "server": config["server"]}


def list_channels(owner_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM notification_channels WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)).fetchall()
    result = []
    for row in rows:
        cfg = decrypt_json(row["config_enc"])
        result.append({"id": row["id"], "kind": row["kind"], "topic": cfg.get("topic", ""), "server": cfg.get("server", ""), "enabled": bool(row["enabled"])})
    return result


def remove_channel(owner_id: str, channel_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM notification_channels WHERE id=? AND owner_id=?", (channel_id, owner_id))
    return cur.rowcount > 0


async def _send_ntfy(config: dict[str, Any], title: str, message: str) -> dict[str, Any]:
    server = config.get("server") or DEFAULT_NTFY_SERVER
    topic = config.get("topic", "")
    if not topic:
        return {"status": "error", "error": "ntfy topic is not set"}
    url = f"{server}/{topic}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, content=message.encode("utf-8"), headers={"Title": title.encode("ascii", "ignore").decode("ascii") or "ATHENA"})
        if response.status_code >= 400:
            return {"status": "error", "error": f"ntfy returned HTTP {response.status_code}"}
        return {"status": "sent", "server": server, "topic": topic}
    except httpx.RequestError as exc:
        return {"status": "error", "error": str(exc)}


async def send_notification(owner_id: str, title: str, message: str) -> list[dict[str, Any]]:
    """Fan out to every enabled channel this user has. Returns one result per
    channel — a failed channel never raises, it just reports its own failure,
    so one broken channel does not silently swallow the others."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM notification_channels WHERE owner_id=? AND enabled=1", (owner_id,)).fetchall()
    if not rows:
        return [{"status": "no_channels_configured"}]
    results = []
    for row in rows:
        cfg = decrypt_json(row["config_enc"])
        if row["kind"] == "ntfy":
            results.append(await _send_ntfy(cfg, title, message))
        else:
            results.append({"status": "error", "error": f"unsupported channel kind {row['kind']}"})
    return results
