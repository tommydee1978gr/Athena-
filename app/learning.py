"""Daily self-reflection — the safe tier of "learn every day".

Once a day, per active user, ATHENA looks at what actually happened (audit
log, which routines fired and how they went) and writes a short private
memory entry about it. If it thinks a routine is worth adding, it creates one
— always disabled, always source='athena_proposed'. A human has to look at it
in the routines list and switch it on. Nothing here ever edits or deletes an
existing routine, and nothing here executes anything outside of writing a
memory row and (optionally) an inert, disabled routine row.

This is deliberately NOT the "call Claude Code to rewrite my own source"
version Tommy asked about. That's a separate, much bigger decision — this
module is the safe half he can have without waiting on that one.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .cliproxy import CLIProxyError, automatic_route, chat_completions
from .db import connect, utcnow
from .memory import add_memory
from .routines import create_routine

logger = logging.getLogger("athena.learning")

_SUGGESTION_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are ATHENA reflecting privately on one user's last 24 hours, for your own record — "
    "this is not shown to the user directly, it becomes a private memory entry. Be concrete and "
    "brief: 2-4 sentences, in Greek, about what actually happened (what routines ran and how they "
    "went, what the user asked about repeatedly, anything that failed). Do not invent anything not "
    "present in the data given to you. If nothing meaningful happened, say so in one sentence.\n\n"
    "If — and only if — you see a clear, concrete case for a new recurring routine (something that "
    "happened repeatedly enough that automating it would obviously help), append a fenced json block "
    "after your reflection with this exact shape, otherwise omit the block entirely:\n"
    '```json\n{"suggested_routines": [{"title": "...", "trigger_type": "cron|interval_minutes", '
    '"trigger_config": {}, "action_type": "ask|notify", "action_config": {}, "reason": "..."}]}\n```\n'
    "Suggest at most one routine. A vague hunch is not a clear case — when in doubt, suggest nothing."
)


def _gather_signal(user_id: str, hours: int = 24) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect() as conn:
        audit_rows = conn.execute(
            "SELECT action, details_json, created_at FROM audit_log WHERE user_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 200",
            (user_id, since),
        ).fetchall()
        routine_rows = conn.execute(
            "SELECT title, trigger_type, action_type, last_run_at, last_result_json FROM routines WHERE owner_id=? AND last_run_at>=?",
            (user_id, since),
        ).fetchall()
        existing_titles = [r["title"] for r in conn.execute("SELECT title FROM routines WHERE owner_id=?", (user_id,)).fetchall()]
    return {
        "audit_events": [{"action": r["action"], "details": _safe_json(r["details_json"]), "at": r["created_at"]} for r in audit_rows],
        "routine_runs": [
            {"title": r["title"], "trigger_type": r["trigger_type"], "action_type": r["action_type"], "ran_at": r["last_run_at"], "result": _safe_json(r["last_result_json"])}
            for r in routine_rows
        ],
        "existing_routine_titles": existing_titles,
    }


def _safe_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _parse_suggestions(text: str) -> tuple[str, list[dict[str, Any]]]:
    match = _SUGGESTION_BLOCK.search(text)
    if not match:
        return text.strip(), []
    reflection = text[: match.start()].strip()
    try:
        parsed = json.loads(match.group(1))
        suggestions = parsed.get("suggested_routines", [])
        if not isinstance(suggestions, list):
            suggestions = []
    except ValueError:
        suggestions = []
    return reflection, suggestions


async def run_daily_reflection(user_id: str) -> dict[str, Any]:
    signal = _gather_signal(user_id)
    if not signal["audit_events"] and not signal["routine_runs"]:
        return {"status": "skipped", "reason": "no activity in the last 24h"}

    question = "Daily reflection over ATHENA's own audit log and routine runs for one user."
    try:
        route = await automatic_route(question)
    except CLIProxyError as exc:
        logger.info("daily reflection skipped for %s: brain unavailable (%s)", user_id, exc.message)
        return {"status": "brain_unavailable", "error": exc.message}

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(signal, ensure_ascii=False, default=str)[:12000]},
    ]
    for model in [route["primary_model"], *route["fallback_models"]]:
        try:
            response = await chat_completions({"messages": messages, "model": model, "temperature": 0.3})
        except CLIProxyError:
            continue
        if response.status_code >= 400:
            continue
        try:
            content = response.json()["choices"][0]["message"].get("content", "")
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if not content.strip():
            continue
        reflection, raw_suggestions = _parse_suggestions(content)
        if reflection:
            try:
                add_memory(user_id, "private", reflection, {"source": "daily_reflection", "date": utcnow()[:10]})
            except Exception as exc:
                logger.info("daily reflection memory save skipped for %s: %s", user_id, exc)
        created = []
        for suggestion in raw_suggestions[:1]:  # at most one, even if the model ignores the instruction
            try:
                routine = create_routine(
                    user_id,
                    str(suggestion.get("title", "Προτεινόμενη ρουτίνα")),
                    str(suggestion.get("trigger_type", "")),
                    suggestion.get("trigger_config") or {},
                    str(suggestion.get("action_type", "")),
                    suggestion.get("action_config") or {},
                    source="athena_proposed",
                    enabled=False,
                )
                created.append({"routine_id": routine["id"], "title": routine["title"], "reason": suggestion.get("reason", "")})
            except ValueError as exc:
                logger.info("discarded malformed routine suggestion for %s: %s", user_id, exc)
        return {"status": "reflected", "reflection": reflection, "suggested_routines": created}
    return {"status": "brain_unavailable", "error": "all models failed"}


async def run_daily_reflection_for_all_users() -> None:
    with connect() as conn:
        user_ids = [row["id"] for row in conn.execute("SELECT id FROM users WHERE active=1").fetchall()]
    for user_id in user_ids:
        try:
            result = await run_daily_reflection(user_id)
            logger.info("daily reflection for %s: %s", user_id, result.get("status"))
        except Exception:
            logger.exception("daily reflection crashed for user %s", user_id)
