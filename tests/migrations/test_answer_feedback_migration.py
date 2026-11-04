"""Exercise AI-135 upgrade/backfill/downgrade on a real pre-feature schema."""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.skipif(not os.getenv("AI135_TEST_DATABASE_URL"), reason="PostgreSQL test URL required")
def test_feedback_migration_backfills_only_unambiguous_pairs():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0035_answer_feedback_ticket.py"
    spec = importlib.util.spec_from_file_location("answer_feedback_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(os.environ["AI135_TEST_DATABASE_URL"])
    schema = "ai135_migration_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(sa.text("CREATE TABLE chat_message (id UUID PRIMARY KEY, session_id UUID, role TEXT, created_at TIMESTAMP)"))
        connection.execute(sa.text("CREATE TABLE ticket (id UUID PRIMARY KEY)"))
        ids = [uuid.uuid4() for _ in range(5)]
        session_id = uuid.uuid4()
        for message_id, role, created_at in zip(ids, ["USER", "ASSISTANT", "USER", "USER", "ASSISTANT"],
                                               ["2026-01-01 00:00:00", "2026-01-01 00:00:01", "2026-01-01 00:00:02", "2026-01-01 00:00:02", "2026-01-01 00:00:03"]):
            connection.execute(sa.text("INSERT INTO chat_message VALUES (:id, :session, :role, :time)"),
                               {"id": message_id, "session": session_id, "role": role, "time": created_at})
        ticket_id = uuid.uuid4()
        connection.execute(sa.text("INSERT INTO ticket VALUES (:id)"), {"id": ticket_id})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = connection.execute(sa.text("SELECT id, parent_message_id, feedback FROM chat_message")).all()
        by_id = {row.id: row for row in rows}
        assert by_id[ids[1]].parent_message_id == ids[0]
        assert by_id[ids[4]].parent_message_id is None
        assert all(row.feedback is None for row in rows)
        assert connection.scalar(sa.text("SELECT citations FROM ticket")) == []
        connection.execute(sa.text("UPDATE ticket SET source_message_id = :id"), {"id": ids[1]})
        connection.execute(sa.text("DELETE FROM chat_message WHERE id = :id"), {"id": ids[1]})
        assert connection.scalar(sa.text("SELECT source_message_id FROM ticket")) is None
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "feedback" not in {column["name"] for column in sa.inspect(connection).get_columns("chat_message")}
        connection.execute(sa.text("SET LOCAL search_path TO public"))
        connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()
