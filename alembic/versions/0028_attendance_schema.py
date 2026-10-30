"""Add schedule, session, attendance, holiday and ticket handover tables (AI-155).

``class_schedule`` holds the recurring weekly template; ``class_session`` is one
dated occurrence materialised from it (or created ad-hoc). Ticket handover
carries ``ticket_id`` without a foreign key until the ticket table lands in
AI-81/AI-83.

Revision ID: 0028_attendance_schema
Revises: 0027_gemini_key_quota
Create Date: 2026-10-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0028_attendance_schema"
down_revision = "0027_gemini_key_quota"
branch_labels = None
depends_on = None

_NEW_TABLES = ("class_schedule", "class_session", "attendance", "holiday", "ticket_handover")


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "class_schedule" not in existing:
        op.create_table(
            "class_schedule",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("class_id", sa.Uuid(), nullable=False),
            sa.Column("subject", sa.String(length=100), nullable=False),
            sa.Column("weekday", sa.SmallInteger(), nullable=False),
            sa.Column("start_time", sa.Time(), nullable=False),
            sa.Column("end_time", sa.Time(), nullable=False),
            sa.Column("trainer_id", sa.Uuid(), nullable=False),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column("effective_to", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_class_schedule_weekday"),
            sa.CheckConstraint("end_time > start_time", name="ck_class_schedule_times"),
            sa.CheckConstraint(
                "effective_to IS NULL OR effective_to >= effective_from",
                name="ck_class_schedule_effective",
            ),
            sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["trainer_id"], ["user.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_class_schedule_trainer_id", "class_schedule", ["trainer_id"])
        op.create_index(
            "ix_class_schedule_class_weekday", "class_schedule", ["class_id", "weekday"]
        )

    if "class_session" not in existing:
        op.create_table(
            "class_session",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("class_id", sa.Uuid(), nullable=False),
            sa.Column("schedule_id", sa.Uuid(), nullable=True),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trainer_id", sa.Uuid(), nullable=False),
            sa.Column(
                "status", sa.String(length=20), nullable=False, server_default="scheduled"
            ),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "status IN ('scheduled', 'cancelled', 'rescheduled')",
                name="ck_class_session_status",
            ),
            sa.CheckConstraint("end_at > start_at", name="ck_class_session_times"),
            sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["schedule_id"], ["class_schedule.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["trainer_id"], ["user.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("class_id", "date", "start_at", name="uq_class_session_slot"),
        )
        op.create_index("ix_class_session_schedule_id", "class_session", ["schedule_id"])
        op.create_index("ix_class_session_trainer_id", "class_session", ["trainer_id"])
        op.create_index("ix_class_session_class_date", "class_session", ["class_id", "date"])
        op.create_index("ix_class_session_date", "class_session", ["date"])

    if "attendance" not in existing:
        op.create_table(
            "attendance",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("class_session_id", sa.Uuid(), nullable=False),
            sa.Column("student_id", sa.Uuid(), nullable=True),
            sa.Column("trainer_id", sa.Uuid(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("recorded_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "(student_id IS NOT NULL) <> (trainer_id IS NOT NULL)",
                name="ck_attendance_person",
            ),
            sa.CheckConstraint(
                "status IN ('present', 'excused_absence', 'unexcused_absence', 'late')",
                name="ck_attendance_status",
            ),
            sa.ForeignKeyConstraint(["class_session_id"], ["class_session.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["student_id"], ["user.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["trainer_id"], ["user.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["recorded_by"], ["user.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_attendance_class_session_id", "attendance", ["class_session_id"])
        op.create_index("ix_attendance_student", "attendance", ["student_id"])
        op.create_index(
            "uq_attendance_session_student",
            "attendance",
            ["class_session_id", "student_id"],
            unique=True,
            postgresql_where=sa.text("student_id IS NOT NULL"),
            sqlite_where=sa.text("student_id IS NOT NULL"),
        )
        op.create_index(
            "uq_attendance_session_trainer",
            "attendance",
            ["class_session_id", "trainer_id"],
            unique=True,
            postgresql_where=sa.text("trainer_id IS NOT NULL"),
            sqlite_where=sa.text("trainer_id IS NOT NULL"),
        )

    if "holiday" not in existing:
        op.create_table(
            "holiday",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("type", sa.String(length=20), nullable=False),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("class_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("type IN ('solar', 'lunar', 'admin')", name="ck_holiday_type"),
            sa.CheckConstraint("scope IN ('system', 'class')", name="ck_holiday_scope"),
            sa.CheckConstraint(
                "(scope = 'class' AND class_id IS NOT NULL)"
                " OR (scope = 'system' AND class_id IS NULL)",
                name="ck_holiday_scope_class",
            ),
            sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_holiday_class_id", "holiday", ["class_id"])
        op.create_index("ix_holiday_date", "holiday", ["date"])
        op.create_index(
            "uq_holiday_system_date",
            "holiday",
            ["date"],
            unique=True,
            postgresql_where=sa.text("class_id IS NULL"),
            sqlite_where=sa.text("class_id IS NULL"),
        )
        op.create_index(
            "uq_holiday_class_date",
            "holiday",
            ["class_id", "date"],
            unique=True,
            postgresql_where=sa.text("class_id IS NOT NULL"),
            sqlite_where=sa.text("class_id IS NOT NULL"),
        )

    if "ticket_handover" not in existing:
        op.create_table(
            "ticket_handover",
            sa.Column("id", sa.Uuid(), nullable=False),
            # No FK yet: the ``ticket`` table arrives with AI-81/AI-83.
            sa.Column("ticket_id", sa.Uuid(), nullable=False),
            sa.Column("from_trainer_id", sa.Uuid(), nullable=False),
            sa.Column("to_trainer_id", sa.Uuid(), nullable=False),
            sa.Column("reason", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "from_trainer_id <> to_trainer_id", name="ck_ticket_handover_same_trainer"
            ),
            sa.CheckConstraint(
                "reason IN ('auto_absent', 'manual_transfer')", name="ck_ticket_handover_reason"
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'accepted', 'rejected')",
                name="ck_ticket_handover_status",
            ),
            sa.ForeignKeyConstraint(["from_trainer_id"], ["user.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["to_trainer_id"], ["user.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ticket_handover_ticket", "ticket_handover", ["ticket_id"])
        op.create_index(
            "ix_ticket_handover_to_status", "ticket_handover", ["to_trainer_id", "status"]
        )
        op.create_index(
            "uq_ticket_handover_pending",
            "ticket_handover",
            ["ticket_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(_NEW_TABLES):
        if table in existing:
            op.drop_table(table)
