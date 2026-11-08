"""Container entrypoint: bring the DB to head, then hand off to uvicorn.

Kept in Python (not a .sh) so it survives Windows checkouts with CRLF line
endings. Alembic migration failures abort boot on purpose: a half-migrated
schema is worse than a container that exits with a clear error.
"""

import os
import subprocess
import sys

import sqlalchemy as sa

from app.config import settings
from app.core.database import create_all_tables


def _has_alembic_version(url: str) -> bool:
    engine = sa.create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version'"
                if not url.startswith("sqlite")
                else "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            return row is not None
    finally:
        engine.dispose()


def _bring_to_head() -> None:
    """Fresh/legacy DBs are built by create_all then stamped; tracked DBs upgrade.

    0001_initial_schema creates every table from the *current* models, so the
    revision chain below it is written for pre-existing databases, not for a
    blank one. A DB without alembic_version therefore just needs create_all +
    ``stamp head`` — running the chain would replay column/constraint steps
    that create_all already applied.
    """
    url = settings.database_url
    if _has_alembic_version(url):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        return

    create_all_tables(url)
    subprocess.run([sys.executable, "-m", "alembic", "stamp", "head"], check=True)


if __name__ == "__main__":
    try:
        _bring_to_head()
    except subprocess.CalledProcessError:
        print(
            "[entrypoint] alembic failed; migrations left the DB in a partial state.",
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
