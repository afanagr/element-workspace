"""Container entrypoint: run migrations, then start uvicorn."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    print("[entrypoint] applying database migrations...", flush=True)
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode != 0:
        print(result.stderr or "migration failed", file=sys.stderr, flush=True)
        return result.returncode

    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    print(f"[entrypoint] starting API on 0.0.0.0:{port}", flush=True)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=log_level,
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
