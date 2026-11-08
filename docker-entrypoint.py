"""Container entrypoint: bring the DB to head, then hand off to uvicorn.

Kept in Python (not a .sh) so it survives Windows checkouts with CRLF line
endings. Alembic migration failures abort boot on purpose: a half-migrated
schema is worse than a container that exits with a clear error.
"""

import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect

db_url = os.environ.get("DATABASE_URL", "")
if db_url:
    try:
        eng = create_engine(db_url)
        insp = inspect(eng)
        tables = insp.get_table_names()
        if "alembic_version" not in tables:
            # Fresh DB: create all tables directly from models and stamp head
            import app.models  # noqa: F401
            from app.core.database import Base

            Base.metadata.create_all(bind=eng)
            subprocess.run([sys.executable, "-m", "alembic", "stamp", "head"], check=True)
        else:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    except Exception as exc:
        print(f"[entrypoint] migration / schema init failed: {exc}", file=sys.stderr)
        raise
else:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)

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
