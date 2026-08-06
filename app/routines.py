"""CRUD for routines — the trigger→action rows the scheduler (app/scheduler.py)
executes. This module never touches APScheduler directly; scheduler.py reads
these rows and owns the actual timing. Keeping the split means routines.py has
no side effects beyond the database, which makes it safe to call from a
request handler or from orchestrator.py's tool-calling loop alike.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import connect, utcnow

TRIGGER_TYPES = {"cron", "interval_minutes"}
ACTION_TYPES = {"ask", "notify"}


def _validate_trigger(trigger_type: str, trigger_config: dict[str, Any]) -> None:
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError("trigger_type must be cron or interval_minutes")
    if trigger_type == "cron" and not trigger_config.get("cron"):
        raise ValueError("cron trigger requires trigger_config.cron, e.g. '0 7 * * *'")
    if trigger_type == "interval_minutes":
        minutes = trigger_config.get("minutes")
        if not isinstance(minutes, int) or minutes < 5:
            raise ValueError("interval_minutes trigger requires trigger_config.minutes >= 5")


def _validate_action(action_type: str, action_config: dict[str, Any]) -> None:
    if action_type not in ACTION_TYPES:
        raise ValueError("action_type must be ask or notify")
    if action_type == "ask" and not str(action_config.get("prompt", "")).strip():
        raise ValueError("ask action requires action_config.prompt")
    if action_type == "notify" and not str(action_config.get("message", "")).strip():
        raise ValueError("notify action requires action_config.message")


def create_routine(
    owner_id: str,
    title: str,
    trigger_type: str,
    trigger_config: dict[str, Any],
    action_type: str,
    action_config: dict[str, Any],
    *,
    source: str = "user",
    enabled: bool = True,
) -> dict[str, Any]:
    _validate_trigger(trigger_type, trigger_config)
    _validate_action(action_type, action_config)
    routine_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO routines(id,owner_id,title,trigger_type,trigger_config_json,action_type,action_config_json,enabled,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (routine_id, owner_id, title.strip()[:200], trigger_type, json.dumps(trigger_config), action_type, json.dumps(action_config), int(enabled), source, now, now),
        )
    return get_routine(routine_id)


def get_routine(routine_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM routines WHERE id=?", (routine_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_routines(owner_id: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM routines"
    conditions = []
    params: list[Any] = []
    if owner_id is not None:
        conditions.append("owner_id=?")
        params.append(owner_id)
    if enabled_only:
        conditions.append("enabled=1")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def set_enabled(owner_id: str, routine_id: str, enabled: bool, is_admin: bool = False) -> bool:
    with connect() as conn:
        if is_admin:
            cur = conn.execute("UPDATE routines SET enabled=?, updated_at=? WHERE id=?", (int(enabled), utcnow(), routine_id))
        else:
            cur = conn.execute("UPDATE routines SET enabled=?, updated_at=? WHERE id=? AND owner_id=?", (int(enabled), utcnow(), routine_id, owner_id))
    return cur.rowcount > 0


def delete_routine(owner_id: str, routine_id: str, is_admin: bool = False) -> bool:
    with connect() as conn:
        if is_admin:
            cur = conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))
        else:
            cur = conn.execute("DELETE FROM routines WHERE id=? AND owner_id=?", (routine_id, owner_id))
    return cur.rowcount > 0


def record_run_result(routine_id: str, result: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE routines SET last_run_at=?, last_result_json=?, updated_at=? WHERE id=?",
            (utcnow(), json.dumps(result, ensure_ascii=False, default=str)[:8000], utcnow(), routine_id),
        )


def _row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["trigger_config"] = json.loads(data.pop("trigger_config_json"))
    data["action_config"] = json.loads(data.pop("action_config_json"))
    if data.get("last_result_json"):
        try:
            data["last_result"] = json.loads(data["last_result_json"])
        except ValueError:
            data["last_result"] = None
    else:
        data["last_result"] = None
    data.pop("last_result_json", None)
    data["enabled"] = bool(data["enabled"])
    return data
