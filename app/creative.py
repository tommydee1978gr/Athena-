"""Project + prompt log for AI music/video work (Suno, Higgsfield, OpenArt).

This is deliberately small: a project is a song/videoclip Tommy (or anyone in
the family with creative.write) is working on, and a prompt is one attempt on
one platform against that project. craft_prompt in orchestrator.py reads this
history so suggestions are grounded in what has actually worked before,
instead of the model inventing a plausible-sounding prompt from nothing.
"""
from __future__ import annotations

import uuid
from typing import Any

from .db import connect, utcnow

PLATFORMS = {"suno", "higgsfield", "openart", "other"}
STATUSES = {"idea", "writing", "generating", "mixing", "done", "published", "abandoned"}
OUTCOMES = {"untried", "worked", "discarded"}


def create_project(owner_id: str, title: str, kind: str = "song", notes: str = "") -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO creative_projects(id,owner_id,title,kind,status,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, owner_id, title.strip()[:200], kind if kind in {"song", "videoclip", "combo", "other"} else "song", "idea", notes.strip()[:4000], now, now),
        )
    return {"id": project_id, "title": title, "kind": kind, "status": "idea"}


def update_project_status(owner_id: str, project_id: str, status: str, is_admin: bool = False) -> bool:
    if status not in STATUSES:
        raise ValueError("Invalid project status")
    with connect() as conn:
        if is_admin:
            cur = conn.execute("UPDATE creative_projects SET status=?, updated_at=? WHERE id=?", (status, utcnow(), project_id))
        else:
            cur = conn.execute("UPDATE creative_projects SET status=?, updated_at=? WHERE id=? AND owner_id=?", (status, utcnow(), project_id, owner_id))
    return cur.rowcount > 0


def add_prompt(owner_id: str, project_id: str, platform: str, prompt_text: str, outcome: str = "untried", notes: str = "") -> dict[str, Any]:
    if platform not in PLATFORMS:
        raise ValueError("Invalid platform")
    if outcome not in OUTCOMES:
        raise ValueError("Invalid outcome")
    with connect() as conn:
        owned = conn.execute("SELECT 1 FROM creative_projects WHERE id=? AND owner_id=?", (project_id, owner_id)).fetchone()
        if not owned:
            raise PermissionError("Project not found or not owned by this user")
        prompt_id = str(uuid.uuid4())
        now = utcnow()
        conn.execute(
            "INSERT INTO creative_prompts(id,project_id,platform,prompt_text,outcome,notes,created_at) VALUES(?,?,?,?,?,?,?)",
            (prompt_id, project_id, platform, prompt_text.strip()[:4000], outcome, notes.strip()[:2000], now),
        )
        conn.execute("UPDATE creative_projects SET updated_at=? WHERE id=?", (now, project_id))
    return {"id": prompt_id, "project_id": project_id, "platform": platform, "outcome": outcome}


def list_projects(owner_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM creative_projects WHERE owner_id=? AND status=? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, status, max(1, min(limit, 200))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM creative_projects WHERE owner_id=? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, max(1, min(limit, 200))),
            ).fetchall()
    return [dict(r) for r in rows]


def get_project(owner_id: str, project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        project = conn.execute("SELECT * FROM creative_projects WHERE id=? AND owner_id=?", (project_id, owner_id)).fetchone()
        if not project:
            return None
        prompts = conn.execute(
            "SELECT * FROM creative_prompts WHERE project_id=? ORDER BY created_at DESC", (project_id,)
        ).fetchall()
    result = dict(project)
    result["prompts"] = [dict(p) for p in prompts]
    return result


def prompts_that_worked(owner_id: str, platform: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Feed for craft_prompt: past prompts marked 'worked', newest first, so the
    model can riff on what has actually been effective instead of guessing."""
    with connect() as conn:
        if platform and platform in PLATFORMS:
            rows = conn.execute(
                """SELECT cp.*, p.title AS project_title FROM creative_prompts cp
                   JOIN creative_projects p ON p.id = cp.project_id
                   WHERE p.owner_id=? AND cp.outcome='worked' AND cp.platform=?
                   ORDER BY cp.created_at DESC LIMIT ?""",
                (owner_id, platform, max(1, min(limit, 30))),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT cp.*, p.title AS project_title FROM creative_prompts cp
                   JOIN creative_projects p ON p.id = cp.project_id
                   WHERE p.owner_id=? AND cp.outcome='worked'
                   ORDER BY cp.created_at DESC LIMIT ?""",
                (owner_id, max(1, min(limit, 30))),
            ).fetchall()
    return [dict(r) for r in rows]
