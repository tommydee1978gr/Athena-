from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME = Path(tempfile.mkdtemp(prefix="athena-browser-js-"))
os.environ["ATHENA_CONFIG_DIR"] = str(RUNTIME / "config")
os.environ["ATHENA_MEDIA_DIR"] = str(RUNTIME / "media")
os.environ["ATHENA_COOKIE_SECURE"] = "0"
os.environ.pop("ATHENA_PUBLIC_BASE_URL", None)

from fastapi.testclient import TestClient

from app.main import app

ADMIN_PASSWORD = "Athena-Browser-JS-2026!"
SCRIPT_RE = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def main() -> int:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node executable is required for browser JavaScript syntax validation")

    with TestClient(app) as client:
        setup = client.post(
            "/setup",
            data={
                "display_name": "Browser JS Validator",
                "username": "browserjs",
                "password": ADMIN_PASSWORD,
            },
            follow_redirects=False,
        )
        if setup.status_code != 303:
            raise SystemExit(f"setup failed: {setup.status_code} {setup.text}")
        response = client.get("/")
        if response.status_code != 200:
            raise SystemExit(f"dashboard failed: {response.status_code} {response.text}")

    scripts = SCRIPT_RE.findall(response.text)
    if not scripts:
        raise SystemExit("no inline JavaScript found in dashboard HTML")

    failures: list[str] = []
    for index, script in enumerate(scripts, start=1):
        path = RUNTIME / f"dashboard-script-{index}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        print(f"script={index} returncode={result.returncode}")
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            failures.append(str(path))

    if failures:
        raise SystemExit(f"browser JavaScript syntax validation failed: {failures}")
    print(f"BROWSER_JS_SYNTAX_OK scripts={len(scripts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
