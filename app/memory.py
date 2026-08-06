from __future__ import annotations

import json
import math
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from .config import MODEL_DIR
from .db import connect, utcnow

_model = None
_model_lock = threading.Lock()
_model_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-model-load")
# Model load timeout: first call caches the model to disk (MODEL_DIR/embeddings) so this
# path is only hit again if that cache is missing/wiped. A HuggingFace network hiccup must
# never hang the whole /api/ask turn for a minute+ waiting on huggingface_hub's internal
# retry backoff — bail out fast and let the caller's except-block degrade gracefully instead.
_MODEL_LOAD_TIMEOUT_S = 8


def _load_model_blocking():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", cache_folder=str(MODEL_DIR / "embeddings"))


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "5")
            future = _model_executor.submit(_load_model_blocking)
            try:
                _model = future.result(timeout=_MODEL_LOAD_TIMEOUT_S)
            except FutureTimeoutError as exc:
                raise RuntimeError("Embedding model load timed out (network to HuggingFace unavailable?)") from exc
    return _model


def embed_text(text: str) -> list[float]:
    model = _load_model()
    vector = model.encode([text], normalize_embeddings=True)[0]
    return [float(v) for v in vector]


def add_memory(owner_id: str | None, namespace: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if namespace not in {"private", "family_shared", "project_shared", "system"}:
        raise ValueError("Invalid memory namespace")
    vector = embed_text(text)
    memory_id = str(uuid.uuid4())
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO memories(id,owner_id,namespace,text,metadata_json,embedding_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (memory_id, owner_id, namespace, text, json.dumps(metadata or {}, ensure_ascii=False), json.dumps(vector), now, now),
        )
    return {"id": memory_id, "namespace": namespace, "text": text, "metadata": metadata or {}, "created_at": now}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / max(1e-12, math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def search_memory(user_id: str, query: str, namespaces: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
    namespaces = namespaces or ["private", "family_shared", "project_shared", "system"]
    allowed = [n for n in namespaces if n in {"private", "family_shared", "project_shared", "system"}]
    if not allowed:
        return []
    query_vector = embed_text(query)
    placeholders = ",".join("?" for _ in allowed)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM memories WHERE namespace IN ({placeholders})
                AND (namespace!='private' OR owner_id=?) ORDER BY updated_at DESC LIMIT 500""",
            (*allowed, user_id),
        ).fetchall()
    scored = []
    for row in rows:
        try:
            vector = json.loads(row["embedding_json"] or "[]")
            score = _cosine(query_vector, vector)
        except (ValueError, TypeError):
            continue
        scored.append({"id": row["id"], "namespace": row["namespace"], "text": row["text"], "metadata": json.loads(row["metadata_json"]), "score": score, "created_at": row["created_at"]})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(1, min(limit, 50))]


def delete_memory(user_id: str, memory_id: str, is_admin: bool = False) -> bool:
    with connect() as conn:
        if is_admin:
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        else:
            cur = conn.execute("DELETE FROM memories WHERE id=? AND owner_id=?", (memory_id, user_id))
    return cur.rowcount > 0
