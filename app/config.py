from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ATHENA"
APP_VERSION = "2.4.0-family-brain-router"

# Local dev default: a folder next to the project (works on Windows/macOS/Linux
# without root permissions). The Dockerfile always sets ATHENA_CONFIG_DIR=/config
# explicitly for the real Unraid deployment, so this default only matters when
# running `uvicorn app.main:app` directly on a workstation.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("ATHENA_CONFIG_DIR", str(_PROJECT_ROOT / "config")))
MEDIA_DIR = Path(os.getenv("ATHENA_MEDIA_DIR", str(_PROJECT_ROOT / "media")))
UI_DIR = _PROJECT_ROOT / "ui"
# Read-only mount of an existing share (lyrics, notes, reference files for
# in-progress projects). Optional — if the path doesn't exist, the
# project_files tools just report nothing is mounted rather than erroring.
PROJECTS_SOURCE_DIR = Path(os.getenv("ATHENA_PROJECTS_DIR", "/projects"))
DB_PATH = CONFIG_DIR / "athena.db"
MASTER_KEY_PATH = CONFIG_DIR / "master.key"
MODEL_DIR = CONFIG_DIR / "models"
RELEASE_DIR = CONFIG_DIR / "releases"
TEMP_DIR = CONFIG_DIR / "tmp"
CLIPROXY_DIR = CONFIG_DIR / "cliproxy"
CLIPROXY_CONFIG_PATH = CLIPROXY_DIR / "config.yaml"
CLIPROXY_SECRETS_PATH = CLIPROXY_DIR / "secrets.json"
CLIPROXY_LOG_DIR = CLIPROXY_DIR / "logs"
SESSION_COOKIE = "athena_session"
SESSION_TTL_SECONDS = int(os.getenv("ATHENA_SESSION_TTL_SECONDS", "43200"))
COOKIE_SECURE = os.getenv("ATHENA_COOKIE_SECURE", "0") == "1"
PUBLIC_BASE_URL = os.getenv("ATHENA_PUBLIC_BASE_URL", "").rstrip("/")
DISTROKID_UPLOAD_URL = "https://distrokid.com/new/"

for path in (CONFIG_DIR, MEDIA_DIR, MODEL_DIR, RELEASE_DIR, TEMP_DIR, CLIPROXY_DIR, CLIPROXY_LOG_DIR):
    path.mkdir(parents=True, exist_ok=True)
