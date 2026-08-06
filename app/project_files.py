"""Read-only access to an existing share mounted at PROJECTS_SOURCE_DIR (e.g.
lyrics, notes, reference files Tommy already has on the Unraid box). This is
deliberately read-only end to end — no write, no delete, no tool that could
touch the mount — the same guarantee the family's real files already had
before ATHENA existed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PROJECTS_SOURCE_DIR

# Skip anything binary/huge — this is for text ATHENA can actually read and
# reason about (lyrics, notes, prompt logs), not a general file browser.
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".lrc", ".srt", ".csv", ".json", ".yaml", ".yml"}
MAX_READ_BYTES = 200_000
MAX_LIST_RESULTS = 200


def mounted() -> bool:
    return PROJECTS_SOURCE_DIR.is_dir()


def _resolve(relative: str) -> Path:
    root = PROJECTS_SOURCE_DIR.resolve()
    candidate = (root / relative.lstrip("/\\")).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionError("Path escapes the mounted projects folder")
    return candidate


def list_files(subpath: str = "", search: str = "") -> list[dict[str, Any]]:
    if not mounted():
        return []
    try:
        base = _resolve(subpath)
    except PermissionError:
        return []
    if not base.exists():
        return []
    results = []
    needle = search.strip().lower()
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(PROJECTS_SOURCE_DIR.resolve()).as_posix()
        if needle and needle not in path.name.lower():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        results.append({"path": relative, "name": path.name, "size_bytes": size, "readable_text": path.suffix.lower() in TEXT_EXTENSIONS})
        if len(results) >= MAX_LIST_RESULTS:
            break
    return results


def read_text_file(relative: str) -> dict[str, Any]:
    if not mounted():
        return {"status": "not_mounted", "error": "No projects folder is mounted for this deployment"}
    try:
        path = _resolve(relative)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    if not path.is_file():
        return {"status": "not_found", "error": "File does not exist"}
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return {"status": "unsupported_type", "error": f"Only text files are readable ({', '.join(sorted(TEXT_EXTENSIONS))})"}
    try:
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return {"status": "too_large", "error": f"File is {size} bytes, over the {MAX_READ_BYTES} limit"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "path": relative, "text": text}
