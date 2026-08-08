""""My room" — a kid-safe, no-confirmation control surface for the exact
handful of Home Assistant devices an admin has assigned to a specific
person (2026-08-08, built for Tasos). Deliberately narrow: a user can only
ever act on the entities in their own row here, enforced by every /api/room/*
route reading this before calling Home Assistant — never an arbitrary
entity_id from the request. Actions execute immediately (no
propose_confirmed_action step) because this is the person directly
operating their own room, the same "autonomous" reasoning ACTION_CAPABILITIES
already uses for Emby/Asterisk in orchestrator.py.

restrict_to_room additionally locks the whole app to /room for that user
(see room_only_gate middleware in app/main.py) — a kiosk mode for a young
child who shouldn't see the rest of ATHENA at all.
"""
from __future__ import annotations

from .db import connect, utcnow

_ROOM_FIELDS = ("power_switch_entity", "power_switch_label", "light_entity", "light_label", "tv_media_entity", "tv_remote_entity", "tv_label")


def get_room_devices(user_id: str) -> dict[str, object]:
    with connect() as conn:
        row = conn.execute(
            """SELECT power_switch_entity, power_switch_label, light_entity, light_label,
                      tv_media_entity, tv_remote_entity, tv_label, restrict_to_room
               FROM user_room_devices WHERE user_id=?""",
            (user_id,),
        ).fetchone()
    if not row:
        return {**{k: "" for k in _ROOM_FIELDS}, "restrict_to_room": False}
    result = dict(row)
    result["restrict_to_room"] = bool(result["restrict_to_room"])
    return result


def set_room_devices(
    user_id: str,
    power_switch_entity: str = "", power_switch_label: str = "",
    light_entity: str = "", light_label: str = "",
    tv_media_entity: str = "", tv_remote_entity: str = "", tv_label: str = "",
) -> dict[str, object]:
    """Updates the device assignment only — leaves restrict_to_room as-is
    (see set_room_restriction), since these are edited from different admin
    forms and neither should clobber the other."""
    existing = get_room_devices(user_id)
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_room_devices(user_id,power_switch_entity,power_switch_label,light_entity,light_label,
                                              tv_media_entity,tv_remote_entity,tv_label,restrict_to_room,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 power_switch_entity=excluded.power_switch_entity, power_switch_label=excluded.power_switch_label,
                 light_entity=excluded.light_entity, light_label=excluded.light_label,
                 tv_media_entity=excluded.tv_media_entity, tv_remote_entity=excluded.tv_remote_entity,
                 tv_label=excluded.tv_label, updated_at=excluded.updated_at""",
            (user_id, power_switch_entity.strip()[:200], power_switch_label.strip()[:60], light_entity.strip()[:200], light_label.strip()[:60],
             tv_media_entity.strip()[:200], tv_remote_entity.strip()[:200], tv_label.strip()[:60], int(existing["restrict_to_room"]), utcnow()),
        )
    return get_room_devices(user_id)


def set_room_restriction(user_id: str, restricted: bool) -> dict[str, object]:
    existing = get_room_devices(user_id)
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_room_devices(user_id,power_switch_entity,power_switch_label,light_entity,light_label,
                                              tv_media_entity,tv_remote_entity,tv_label,restrict_to_room,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET restrict_to_room=excluded.restrict_to_room, updated_at=excluded.updated_at""",
            (user_id, existing["power_switch_entity"], existing["power_switch_label"], existing["light_entity"], existing["light_label"],
             existing["tv_media_entity"], existing["tv_remote_entity"], existing["tv_label"], int(restricted), utcnow()),
        )
    return get_room_devices(user_id)


def has_any_device(room: dict[str, object]) -> bool:
    return bool(room.get("power_switch_entity") or room.get("light_entity") or room.get("tv_media_entity"))


def is_restricted_to_room(user_id: str) -> bool:
    return get_room_devices(user_id)["restrict_to_room"]
