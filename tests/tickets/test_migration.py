"""The additive excerpt migration must preserve existing tickets."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_agent_answer_migration_preserves_existing_ticket():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0029_ticket_agent_answer.py"
    spec = importlib.util.spec_from_file_location("ticket_agent_answer_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ticket (id TEXT PRIMARY KEY, question TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO ticket VALUES ('old-ticket', 'Original question')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("ticket")}
        assert columns["agent_answer"]["nullable"] is True
        assert connection.execute(text(
            "SELECT question, agent_answer FROM ticket WHERE id = 'old-ticket'"
        )).one() == ("Original question", None)
    engine.dispose()
