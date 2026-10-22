"""Container entrypoint: bring the DB to head, then hand off to uvicorn.

Kept in Python (not a .sh) so it survives Windows checkouts with CRLF line
endings. Alembic migration failures abort boot on purpose: a half-migrated
schema is worse than a container that exits with a clear error.
"""

import os
import subprocess
import sys

try:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
except subprocess.CalledProcessError:
    print(
        "[entrypoint] alembic upgrade failed. If this DB predates migrations "
        "(old create_all startup), reset local volumes once with: "
        "docker compose down -v && docker compose up -d --build",
        file=sys.stderr,
    )
    raise

os.execvp(
    sys.executable,
    [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ],
)
