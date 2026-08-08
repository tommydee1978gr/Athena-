"""Personal health/fitness data — fed by a phone-side Health Connect exporter
(e.g. the "Health Connect Webhook" app, itself fed by "Health Sync" bridging
Huawei Health/Fitbit/Garmin/etc into Android's Health Connect). ATHENA never
talks to Huawei/Google/Samsung health APIs directly — those all require being
a published mobile app to get real API access, a dead end for a self-hosted
server. Instead the phone pushes a JSON payload to /api/health/webhook on
its own schedule; this module just stores and summarizes what arrives.

Deliberately private per-user (like memory.private) — see health.read in
permissions.py, not shared to the family by default.
"""
from __future__ import annotations

import json
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import connect, utcnow

# Every key in an incoming payload other than these two is treated as a metric
# type name (steps/sleep/heart_rate/weight/...) whose value is a list of data
# points — see docs/webhook.md in mcnaveen/health-connect-webhook for the full
# 31-type schema. Not hardcoding the type list here on purpose: a phone app
# update adding a new metric type should just work, not need an ATHENA release.
_PAYLOAD_META_KEYS = {"timestamp", "app_version"}
# Tried in this order to find the one timestamp that best represents "when did
# this happen" across wildly different per-type schemas (steps has start_time/
# end_time, weight/heart_rate have time, sleep has session_end_time, ...).
_TIMESTAMP_KEYS = ("time", "start_time", "session_end_time", "end_time")


def _extract_recorded_at(item: dict[str, Any]) -> str | None:
    for key in _TIMESTAMP_KEYS:
        value = item.get(key)
        if value:
            return str(value)
    return None


def ingest_metrics(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Stores every data point in every metric-type array of the payload.
    Best-effort per item — one malformed data point (missing a timestamp)
    is skipped, not a reason to reject the whole batch, since a phone app
    syncing 6 metric types shouldn't lose 5 of them because 1 is odd."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    received_at = utcnow()
    inserted: dict[str, int] = {}
    skipped: dict[str, int] = {}
    with connect() as conn:
        for metric_type, items in payload.items():
            if metric_type in _PAYLOAD_META_KEYS or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    skipped[metric_type] = skipped.get(metric_type, 0) + 1
                    continue
                recorded_at = _extract_recorded_at(item)
                if not recorded_at:
                    skipped[metric_type] = skipped.get(metric_type, 0) + 1
                    continue
                conn.execute(
                    "INSERT INTO health_metrics(id,owner_id,metric_type,value_json,recorded_at,received_at,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), user_id, str(metric_type)[:60], json.dumps(item, ensure_ascii=False), recorded_at, received_at, received_at),
                )
                inserted[metric_type] = inserted.get(metric_type, 0) + 1
    return {"status": "ok", "inserted": inserted, "skipped": skipped}


def _rows(user_id: str, metric_type: str, since: str, limit: int = 2000) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT value_json, recorded_at FROM health_metrics WHERE owner_id=? AND metric_type=? AND recorded_at>=? ORDER BY recorded_at ASC LIMIT ?",
            (user_id, metric_type, since, limit),
        ).fetchall()
    return [{"recorded_at": r["recorded_at"], **json.loads(r["value_json"])} for r in rows]


def _day(recorded_at: str) -> str:
    return recorded_at[:10]  # ISO-8601 date prefix — good enough without full parsing


def daily_summary(user_id: str, days: int = 7) -> dict[str, Any]:
    """A compact, LLM-friendly digest for the health_summary tool — per-day
    totals/averages for the metric types people actually ask about day to
    day, plus a raw count of everything else that's been synced so the
    model knows more is available even if not summarized in depth here."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    summary: dict[str, Any] = {"window_days": days}

    steps = _rows(user_id, "steps", since)
    if steps:
        by_day: dict[str, int] = {}
        for item in steps:
            by_day[_day(item["recorded_at"])] = by_day.get(_day(item["recorded_at"]), 0) + int(item.get("count", 0))
        summary["steps_per_day"] = by_day
        summary["avg_steps_per_day"] = round(sum(by_day.values()) / len(by_day)) if by_day else 0

    sleep = _rows(user_id, "sleep", since)
    if sleep:
        by_day = {}
        for item in sleep:
            hours = round(int(item.get("duration_seconds", 0)) / 3600, 1)
            by_day[_day(item["recorded_at"])] = hours
        summary["sleep_hours_per_night"] = by_day
        summary["avg_sleep_hours"] = round(statistics.mean(by_day.values()), 1) if by_day else 0

    heart_rate = _rows(user_id, "heart_rate", since)
    if heart_rate:
        values = [item["bpm"] for item in heart_rate if "bpm" in item]
        if values:
            summary["heart_rate_bpm"] = {"min": min(values), "max": max(values), "avg": round(statistics.mean(values)), "latest": heart_rate[-1]["bpm"], "sample_count": len(values)}

    weight = _rows(user_id, "weight", since)
    if weight:
        summary["weight_kg"] = {"latest": weight[-1]["kilograms"], "recorded_at": weight[-1]["recorded_at"], "sample_count": len(weight)}

    resting_hr = _rows(user_id, "resting_heart_rate", since)
    if resting_hr and "bpm" in resting_hr[-1]:
        summary["resting_heart_rate_bpm"] = {"latest": resting_hr[-1]["bpm"], "sample_count": len(resting_hr)}

    with connect() as conn:
        other_counts = conn.execute(
            "SELECT metric_type, COUNT(*) AS c FROM health_metrics WHERE owner_id=? AND recorded_at>=? GROUP BY metric_type",
            (user_id, since),
        ).fetchall()
    summary["all_synced_metric_types"] = {row["metric_type"]: row["c"] for row in other_counts}
    summary["has_any_data"] = bool(other_counts)
    return summary
