import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize("storage_path_exists", [True, False])
def test_document_storage_migration_is_idempotent(storage_path_exists):
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0008_document_storage.py"
    )
    spec = importlib.util.spec_from_file_location("document_storage_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE document (id INTEGER PRIMARY KEY" +
            (", storage_path VARCHAR(500)" if storage_path_exists else "") + ")"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("document")}
        assert "storage_path" in columns

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("document")}
        assert "storage_path" not in columns
    engine.dispose()
