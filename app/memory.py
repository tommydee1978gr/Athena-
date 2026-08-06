from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from .db import connect, utcnow

_NAMESPACES = {"private", "family_shared", "project_shared", "system"}


def add_memory(owner_id: str | None, namespace: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if namespace not in _NAMESPACES:
        raise ValueError("Invalid memory namespace")
    memory_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO memories(id,owner_id,namespace,text,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (memory_id, owner_id, namespace, text, json.dumps(metadata or {}, ensure_ascii=False), now, now),
        )
    return {"id": memory_id, "namespace": namespace, "text": text, "metadata": metadata or {}, "created_at": now}


def _fts_match_query(text: str) -> str:
    # Each word becomes its own quoted phrase term (so punctuation/FTS5 operator
    # characters in free-typed queries can't break or inject into the MATCH syntax),
    # OR-joined so any matching word surfaces the memory — plain keyword recall,
    # not semantic similarity, but zero model/network dependency to get it.
    tokens = re.findall(r"\w+", text, re.UNICODE)[:12]
    if not tokens:
        return ""
    return " OR ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)


def search_memory(user_id: str, query: str, namespaces: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
    namespaces = namespaces or list(_NAMESPACES)
    allowed = [n for n in namespaces if n in _NAMESPACES]
    if not allowed:
        return []
    match_query = _fts_match_query(query)
    if not match_query:
        return []
    placeholders = ",".join("?" for _ in allowed)
    with connect() as conn:
        try:
            rows = conn.execute(
                f"""SELECT m.id, m.namespace, m.text, m.metadata_json, m.created_at
                    FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid
                    WHERE memories_fts MATCH ? AND m.namespace IN ({placeholders})
                    AND (m.namespace!='private' OR m.owner_id=?)
                    ORDER BY bm25(memories_fts) LIMIT ?""",
                (match_query, *allowed, user_id, max(1, min(limit, 50))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [
        {"id": row["id"], "namespace": row["namespace"], "text": row["text"], "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"]}
        for row in rows
    ]


def delete_memory(user_id: str, memory_id: str, is_admin: bool = False) -> bool:
    with connect() as conn:
        if is_admin:
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        else:
            cur = conn.execute("DELETE FROM memories WHERE id=? AND owner_id=?", (memory_id, user_id))
    return cur.rowcount > 0
