"""Guards for the alembic revision table contract."""

from alembic.config import Config
from alembic.script import ScriptDirectory

# ``alembic_version.version_num`` is varchar(32); a longer id cannot be stored.
MAX_REVISION_ID_LENGTH = 32


def test_revision_ids_fit_alembic_version_column() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    too_long = {
        revision.revision: len(revision.revision)
        for revision in script.walk_revisions()
        if len(revision.revision) > MAX_REVISION_ID_LENGTH
    }
    assert not too_long, f"Revision ids must be <= {MAX_REVISION_ID_LENGTH} chars: {too_long}"


def test_revision_graph_has_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected one Alembic head, found {sorted(heads)}"
