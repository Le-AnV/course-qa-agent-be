"""Persist answer feedback, question links and independent ticket snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0035_answer_feedback_ticket"
down_revision = "0034_ticket_escalation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_message") as batch:
        batch.add_column(sa.Column("feedback", sa.String(20), nullable=True))
        batch.add_column(sa.Column("parent_message_id", sa.Uuid(), nullable=True))
        batch.create_check_constraint("ck_chat_message_feedback", "feedback IN ('helpful', 'not_helpful')")
        batch.create_foreign_key("fk_chat_message_parent", "chat_message", ["parent_message_id"], ["id"], ondelete="SET NULL")
    # Equal timestamps cannot establish adjacency. Leave ambiguous history unlinked.
    op.execute(sa.text("""
        WITH ordered AS (
            SELECT id, role, created_at,
                lag(id) OVER w AS previous_id,
                lag(role) OVER w AS previous_role,
                lag(created_at) OVER w AS previous_time,
                count(*) OVER (PARTITION BY session_id, created_at) AS same_time
            FROM chat_message
            WINDOW w AS (PARTITION BY session_id ORDER BY created_at, id)
        )
        UPDATE chat_message SET parent_message_id = ordered.previous_id
        FROM ordered
        WHERE chat_message.id = ordered.id AND ordered.role = 'ASSISTANT'
          AND ordered.previous_role = 'USER' AND ordered.same_time = 1
          AND ordered.previous_time < ordered.created_at
          AND (SELECT count(*) FROM chat_message p
               WHERE p.session_id = chat_message.session_id
                 AND p.created_at = ordered.previous_time) = 1
    """))
    conn = op.get_bind()
    ticket_cols = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ticket'"
        ).fetchall()
    }
    with op.batch_alter_table("ticket") as batch:
        if "citations" not in ticket_cols:
            batch.add_column(sa.Column("citations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        if "source_message_id" not in ticket_cols:
            batch.add_column(sa.Column("source_message_id", sa.Uuid(), nullable=True))
            batch.create_foreign_key("fk_ticket_source_message", "chat_message", ["source_message_id"], ["id"], ondelete="SET NULL")
            batch.create_unique_constraint("uq_ticket_source_message_id", ["source_message_id"])


def downgrade() -> None:
    with op.batch_alter_table("ticket") as batch:
        batch.drop_constraint("uq_ticket_source_message_id", type_="unique")
        batch.drop_constraint("fk_ticket_source_message", type_="foreignkey")
        batch.drop_column("source_message_id")
        batch.drop_column("citations")
    with op.batch_alter_table("chat_message") as batch:
        batch.drop_constraint("fk_chat_message_parent", type_="foreignkey")
        batch.drop_constraint("ck_chat_message_feedback", type_="check")
        batch.drop_column("parent_message_id")
        batch.drop_column("feedback")
