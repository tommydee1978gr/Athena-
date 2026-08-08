""""My room" — a kid-safe, no-confirmation control surface for the exact
handful of Home Assistant devices an admin has assigned to a specific
person (2026-08-08, built for Tasos). Deliberately narrow: a user can only
ever act on the entities in their own row here, enforced by every /api/room/*
route reading this before calling Home Assistant — never an arbitrary
entity_id from the request. Actions execute immediately (no
propose_confirmed_action step) because this is the person directly
operating their own room, the same "autonomous" reasoning ACTION_CAPABILITIES
already uses for Emby/Asterisk in orchestrator.py.
"""
from __future__ import annotations

from .db import connect, utcnow


def get_room_devices(user_id: str) -> dict[str, str]:
    with connect() as conn:
        row = conn.execute(
            """SELECT power_switch_entity, power_switch_label, light_entity, light_label,
                      tv_media_entity, tv_remote_entity, tv_label
               FROM user_room_devices WHERE user_id=?""",
            (user_id,),
        ).fetchone()
    if not row:
        return {k: "" for k in ("power_switch_entity", "power_switch_label", "light_entity", "light_label", "tv_media_entity", "tv_remote_entity", "tv_label")}
    return dict(row)


def set_room_devices(
    user_id: str,
    power_switch_entity: str = "", power_switch_label: str = "",
    light_entity: str = "", light_label: str = "",
    tv_media_entity: str = "", tv_remote_entity: str = "", tv_label: str = "",
) -> dict[str, str]:
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_room_devices(user_id,power_switch_entity,power_switch_label,light_entity,light_label,
                                              tv_media_entity,tv_remote_entity,tv_label,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 power_switch_entity=excluded.power_switch_entity, power_switch_label=excluded.power_switch_label,
                 light_entity=excluded.light_entity, light_label=excluded.light_label,
                 tv_media_entity=excluded.tv_media_entity, tv_remote_entity=excluded.tv_remote_entity,
                 tv_label=excluded.tv_label, updated_at=excluded.updated_at""",
            (user_id, power_switch_entity.strip()[:200], power_switch_label.strip()[:60], light_entity.strip()[:200], light_label.strip()[:60],
             tv_media_entity.strip()[:200], tv_remote_entity.strip()[:200], tv_label.strip()[:60], utcnow()),
        )
    return get_room_devices(user_id)


def has_any_device(room: dict[str, str]) -> bool:
    return bool(room.get("power_switch_entity") or room.get("light_entity") or room.get("tv_media_entity"))
