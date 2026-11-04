"""Exercise the AI-155 ticket_handover foreign key migration."""

import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

from alembic import command

PRE_MIGRATION_DDL = (
    'CREATE TABLE "ticket" (id CHAR(32) PRIMARY KEY)',
    (
        'CREATE TABLE "ticket_handover" (id CHAR(32) PRIMARY KEY,'
        " ticket_id CHAR(32) NOT NULL, from_trainer_id CHAR(32) NOT NULL,"
        " to_trainer_id CHAR(32) NOT NULL, reason VARCHAR(20) NOT NULL,"
        " status VARCHAR(20) NOT NULL)"
    ),
)


@pytest.fixture()
def migrated_engine(tmp_path):
    test_db = tmp_path / "handover_fk.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{test_db}")

    engine = create_engine(f"sqlite:///{test_db}")

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with engine.begin() as conn:
        for ddl in PRE_MIGRATION_DDL:
            conn.execute(text(ddl))

    command.stamp(cfg, "0035_answer_feedback_ticket")
    yield engine, cfg
    engine.dispose()


def _insert_handover(engine, ticket_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ticket_handover"
                " (id, ticket_id, from_trainer_id, to_trainer_id, reason, status)"
                " VALUES (:id, :ticket, :from_id, :to_id, 'manual_transfer', 'pending')"
            ),
            {
                "id": uuid.uuid4().hex,
                "ticket": ticket_id,
                "from_id": uuid.uuid4().hex,
                "to_id": uuid.uuid4().hex,
            },
        )


def test_upgrade_rejects_a_handover_for_an_unknown_ticket(migrated_engine):
    engine, cfg = migrated_engine

    # Before the migration the bare UUID column accepts anything.
    _insert_handover(engine, uuid.uuid4().hex)

    command.upgrade(cfg, "0036_ticket_handover_fk")

    with pytest.raises(IntegrityError):
        _insert_handover(engine, uuid.uuid4().hex)


def test_upgrade_accepts_a_handover_for_an_existing_ticket(migrated_engine):
    engine, cfg = migrated_engine
    command.upgrade(cfg, "0036_ticket_handover_fk")

    ticket_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ticket (id) VALUES (:id)"), {"id": ticket_id})

    _insert_handover(engine, ticket_id)


def test_downgrade_drops_the_constraint(migrated_engine):
    engine, cfg = migrated_engine

    command.upgrade(cfg, "0036_ticket_handover_fk")
    command.downgrade(cfg, "0035_answer_feedback_ticket")

    _insert_handover(engine, uuid.uuid4().hex)
