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


def test_reopened_at_migration_adds_an_optional_timestamp():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0033_ticket_reopened_at.py"
    spec = importlib.util.spec_from_file_location("ticket_reopened_at_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ticket (id TEXT PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("ticket")}
        assert columns["reopened_at"]["nullable"] is True
    engine.dispose()


def test_original_question_migration_preserves_existing_ticket():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0030_ticket_original_question.py"
    spec = importlib.util.spec_from_file_location("ticket_original_question_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ticket (id TEXT PRIMARY KEY, question TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO ticket VALUES ('old-ticket', 'Teacher question')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("ticket")}
        assert columns["original_question"]["nullable"] is True
        assert connection.execute(text(
            "SELECT question, original_question FROM ticket WHERE id = 'old-ticket'"
        )).one() == ("Teacher question", None)
    engine.dispose()


def test_student_seen_migration_backfills_existing_tickets():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0038_ticket_student_seen_at.py"
    spec = importlib.util.spec_from_file_location("student_seen_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE ticket (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO ticket (id) VALUES ('old-ticket')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text(
                "SELECT student_seen_at FROM ticket WHERE id = 'old-ticket'"
            )).scalar_one() is not None
            migration.downgrade()
        assert "student_seen_at" not in {
            column["name"] for column in inspect(connection).get_columns("ticket")
        }
    engine.dispose()
