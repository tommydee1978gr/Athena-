"""Read/write access to an existing share mounted at PROJECTS_SOURCE_DIR
(lyrics, notes, storyboard images, reference files). Reading is free —
search_project_files/read_project_file are plain LLM tools. Every write
(write_text_file, make_directory, save_media_as_project_file, move_path) is
never called directly by the model: it only ever runs after Tommy approves
the specific proposal in /actions, via orchestrator.ACTION_CAPABILITIES and
main._execute_proposed_action. This module itself has no confirmation
logic — that boundary lives one layer up, on purpose, so it's enforced the
same way as every other write action in ATHENA.
"""
from __future__ import annotations

import shutil
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


def write_text_file(relative: str, content: str, *, overwrite: bool = False) -> dict[str, Any]:
    if not mounted():
        return {"status": "not_mounted", "error": "No projects folder is mounted for this deployment"}
    try:
        path = _resolve(relative)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    if path.exists() and not overwrite:
        return {"status": "already_exists", "error": "File exists — pass overwrite to replace it"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "path": relative, "bytes_written": len(content.encode("utf-8"))}


def make_directory(relative: str) -> dict[str, Any]:
    if not mounted():
        return {"status": "not_mounted", "error": "No projects folder is mounted for this deployment"}
    try:
        path = _resolve(relative)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "path": relative}


def move_path(source_relative: str, dest_relative: str) -> dict[str, Any]:
    """Rename/organize — moves a file or folder already inside the mount."""
    if not mounted():
        return {"status": "not_mounted", "error": "No projects folder is mounted for this deployment"}
    try:
        source = _resolve(source_relative)
        dest = _resolve(dest_relative)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    if not source.exists():
        return {"status": "not_found", "error": "Source does not exist"}
    if dest.exists():
        return {"status": "already_exists", "error": "Destination already exists"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "from": source_relative, "to": dest_relative}


def save_media_as_project_file(user_id: str, media_id: str, dest_relative: str, *, overwrite: bool = False) -> dict[str, Any]:
    """Copy a file the user already uploaded to their private media library
    (an image from ChatGPT/Imagen/Google Flow, downloaded and uploaded through
    ATHENA) into the right spot on the projects share."""
    from .integrations import safe_media_path  # local import: integrations doesn't import project_files, no cycle
    from .db import connect

    with connect() as conn:
        row = conn.execute("SELECT relative_path,owner_id FROM media_files WHERE id=?", (media_id,)).fetchone()
    if not row or row["owner_id"] != user_id:
        return {"status": "not_found", "error": "Media file not found or not owned by this user"}
    try:
        source_path = safe_media_path(row["relative_path"])
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    try:
        data = source_path.read_bytes()
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return save_bytes(dest_relative, data, overwrite=overwrite)


def save_bytes(relative: str, data: bytes, *, overwrite: bool = False) -> dict[str, Any]:
    """Used to land an already-uploaded image (or anything binary) into the
    share — e.g. a storyboard frame generated elsewhere and uploaded through
    ATHENA's media library, now filed under the right project folder."""
    if not mounted():
        return {"status": "not_mounted", "error": "No projects folder is mounted for this deployment"}
    try:
        path = _resolve(relative)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}
    if path.exists() and not overwrite:
        return {"status": "already_exists", "error": "File exists — pass overwrite to replace it"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "path": relative, "bytes_written": len(data)}
