"""The proactive loop: an in-process APScheduler instance that fires routines
on their own schedule, no request needed. Started/stopped from main.py's
lifespan handler — same process, same container, no new service.

A routine's action runs as the routine's owner, constructed directly from the
users table (there is no HTTP request or session here — this is the one place
in the codebase that calls orchestrator.ask() outside a request).
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .db import connect
from .learning import run_daily_reflection_for_all_users
from .mcp_client import refresh_tool_cache
from .notify import send_notification
from .routines import get_routine, list_routines, record_run_result

logger = logging.getLogger("athena.scheduler")

_scheduler: AsyncIOScheduler | None = None


def _trigger_for(routine: dict[str, Any]):
    config = routine["trigger_config"]
    if routine["trigger_type"] == "cron":
        return CronTrigger.from_crontab(config["cron"])
    if routine["trigger_type"] == "interval_minutes":
        return IntervalTrigger(minutes=int(config["minutes"]))
    raise ValueError(f"Unknown trigger_type {routine['trigger_type']}")


def _owner_user(owner_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT id, display_name, role FROM users WHERE id=? AND active=1", (owner_id,)).fetchone()
    return dict(row) if row else None


async def run_routine(routine_id: str) -> None:
    routine = get_routine(routine_id)
    if not routine or not routine["enabled"]:
        return
    user = _owner_user(routine["owner_id"])
    if not user:
        record_run_result(routine_id, {"status": "error", "error": "owner account no longer exists or is inactive"})
        return
    try:
        if routine["action_type"] == "ask":
            from .orchestrator import ask  # local import: orchestrator imports back into this module's neighbours

            prompt = routine["action_config"]["prompt"]
            result = await ask(user, prompt)
            record_run_result(routine_id, {"status": "ok", "action": "ask", "answer": result.get("answer", "")[:2000]})
            # An ask-routine's whole point is usually to tell someone something —
            # also push the answer through their notification channels so a
            # morning briefing actually reaches a phone, not just a DB row.
            if result.get("answer"):
                await send_notification(routine["owner_id"], routine["title"], result["answer"][:1000])
        elif routine["action_type"] == "notify":
            message = routine["action_config"]["message"]
            results = await send_notification(routine["owner_id"], routine["title"], message)
            record_run_result(routine_id, {"status": "ok", "action": "notify", "channel_results": results})
        else:
            record_run_result(routine_id, {"status": "error", "error": f"unknown action_type {routine['action_type']}"})
    except Exception as exc:  # a broken routine must never take the scheduler down
        logger.exception("routine %s failed", routine_id)
        record_run_result(routine_id, {"status": "error", "error": str(exc)})


def schedule_routine(routine: dict[str, Any]) -> None:
    if _scheduler is None:
        return
    if not routine["enabled"]:
        unschedule_routine(routine["id"])
        return
    _scheduler.add_job(run_routine, trigger=_trigger_for(routine), args=[routine["id"]], id=routine["id"], replace_existing=True, misfire_grace_time=300)


def unschedule_routine(routine_id: str) -> None:
    if _scheduler is not None and _scheduler.get_job(routine_id):
        _scheduler.remove_job(routine_id)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()
    for routine in list_routines(enabled_only=True):
        try:
            schedule_routine(routine)
        except Exception:
            logger.exception("failed to schedule routine %s on startup", routine.get("id"))
    # System-level job, not a user routine: once a day, every active user gets
    # a private reflection entry plus (rarely) a disabled routine suggestion.
    _scheduler.add_job(
        run_daily_reflection_for_all_users, trigger=CronTrigger.from_crontab("0 3 * * *"),
        id="__daily_reflection__", replace_existing=True, misfire_grace_time=3600,
    )
    # Keeps orchestrator's synchronous MCP tool list warm — see mcp_client.py
    # for why discovery has to happen out-of-band from the request path.
    _scheduler.add_job(
        refresh_tool_cache, trigger=IntervalTrigger(minutes=5),
        id="__mcp_tool_refresh__", replace_existing=True, misfire_grace_time=120,
    )
    _scheduler.start()
    _scheduler.add_job(refresh_tool_cache, id="__mcp_tool_refresh_initial__", misfire_grace_time=60)
    logger.info("scheduler started with %d routine(s) + daily reflection + MCP refresh", len(_scheduler.get_jobs()) - 3)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
