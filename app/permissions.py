from __future__ import annotations

from .db import connect, utcnow

ALL_CAPABILITIES = {
    "users.manage", "integrations.configure", "integrations.connect", "memory.private", "memory.family", "memory.project", "memory.system",
    "family.tasks.read", "family.tasks.write", "family.locations.read", "location.share", "voice.use", "voice.enroll",
    "gmail.read", "gmail.send", "calendar.read", "calendar.write", "tasks.read", "tasks.write", "youtube.read", "youtube.publish",
    "spotify.read", "spotify.control", "tiktok.read", "tiktok.publish", "instagram.read", "instagram.publish",
    "homeassistant.read", "homeassistant.control",
    "emby.read", "emby.control", "voip.call", "distrokid.manage", "llm.use", "audit.read",
    "creative.read", "creative.write", "routines.manage", "notifications.manage", "mcp.use",
    "network.read", "network.control", "infra.read", "infra.control",
}

ROLE_DEFAULTS = {
    "admin": ALL_CAPABILITIES,
    # network.*/infra.* (Omada Controller / Unraid server itself) are admin-only by
    # Tommy's own request 2026-08-07 — home network + server infra, not something
    # every adult in the family should touch.
    "adult": ALL_CAPABILITIES - {"users.manage", "integrations.configure", "memory.system", "audit.read", "network.read", "network.control", "infra.read", "infra.control"},
    "child": {
        "integrations.connect", "memory.private", "memory.family", "family.tasks.read", "family.tasks.write", "family.locations.read", "location.share",
        "voice.use", "voice.enroll", "calendar.read", "tasks.read", "tasks.write", "youtube.read", "spotify.read", "emby.read", "llm.use",
        "creative.read", "creative.write",
    },
    "guest": {"memory.private", "family.tasks.read", "voice.use", "llm.use"},
}


def is_allowed(user, capability: str) -> bool:
    if capability not in ALL_CAPABILITIES:
        return False
    with connect() as conn:
        row = conn.execute("SELECT allowed FROM user_permissions WHERE user_id=? AND capability=?", (user["id"], capability)).fetchone()
    if row is not None:
        return bool(row["allowed"])
    return capability in ROLE_DEFAULTS.get(user["role"], set())


def set_permission(user_id: str, capability: str, allowed: bool) -> None:
    if capability not in ALL_CAPABILITIES:
        raise ValueError("Unknown capability")
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_permissions(user_id,capability,allowed,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id,capability) DO UPDATE SET allowed=excluded.allowed,updated_at=excluded.updated_at""",
            (user_id, capability, int(allowed), utcnow()),
        )


def permission_snapshot(user_id: str, role: str) -> dict[str, bool]:
    result = {cap: cap in ROLE_DEFAULTS.get(role, set()) for cap in sorted(ALL_CAPABILITIES)}
    with connect() as conn:
        rows = conn.execute("SELECT capability,allowed FROM user_permissions WHERE user_id=?", (user_id,)).fetchall()
    for row in rows:
        result[row["capability"]] = bool(row["allowed"])
    return result
