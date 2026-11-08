"""Class-scoped skill taxonomy, rubrics, and quiz question binding (AI-Skill-Taxonomy).

Revision ID: 0041_skill_taxonomy
Revises: 0040_quiz_availability
Create Date: 2026-11-06

Downgrade note: Downgrade is LOSSY. 5 difficulty levels are collapsed 
back to 3 levels (NOVICE->REMEMBER, BEGINNER->UNDERSTAND, rest->APPLY), 
the skill_id column is dropped, and skill/rubric records are removed.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0041_skill_taxonomy"
down_revision = "0040_quiz_availability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Tạo bảng skill gắn với class_id
    op.create_table(
        "skill",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("class_id", sa.Uuid(), sa.ForeignKey("class.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(100), nullable=False, server_default="GENERAL"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("class_id", "code", name="uq_skill_class_code"),
    )
    op.create_index("ix_skill_class_id", "skill", ["class_id"])
    op.create_index("ix_skill_category", "skill", ["category"])

    # 2. Tạo bảng tiêu chuẩn Rubric tinh gọn (description + threshold_pct)
    op.create_table(
        "skill_level_rubric",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("skill_id", sa.Uuid(), sa.ForeignKey("skill.id", ondelete="CASCADE"), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("threshold_pct", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("skill_id", "level", name="uq_skill_level_rubric"),
        sa.CheckConstraint(
            "level IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER')",
            name="ck_skill_level_rubric_level",
        ),
    )
    op.create_index("ix_skill_level_rubric_skill_id", "skill_level_rubric", ["skill_id"])

    # 3. Tạo record GENERAL cho mỗi lớp đang có quiz
    op.execute("""
        INSERT INTO skill (id, class_id, code, name, category, description, created_at, updated_at)
        SELECT gen_random_uuid(), c.id, 'GENERAL', 'Kiến thức chung', 'GENERAL', 'Kỹ năng mặc định của lớp', now(), now()
        FROM class c
        WHERE EXISTS (
            SELECT 1 FROM quiz q WHERE q.class_id = c.id
        )
        ON CONFLICT (class_id, code) DO NOTHING;
    """)

    # 4. Thêm skill_id vào quiz_question và backfill từ record GENERAL của lớp tương ứng
    with op.batch_alter_table("quiz_question") as batch:
        batch.add_column(sa.Column("skill_id", sa.Uuid(), nullable=True))

    op.execute("""
        UPDATE quiz_question qq
        SET skill_id = s.id
        FROM quiz q
        JOIN skill s ON s.class_id = q.class_id AND s.code = 'GENERAL'
        WHERE qq.quiz_id = q.id;
    """)

    with op.batch_alter_table("quiz_question") as batch:
        batch.create_foreign_key(
            "fk_quiz_question_skill_id",
            "skill",
            ["skill_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_quiz_question_skill_id", "quiz_question", ["skill_id"])

    # 5. Sửa Check Constraint difficulty (DROP -> UPDATE -> ADD)
    op.execute("ALTER TABLE quiz_question DROP CONSTRAINT IF EXISTS ck_quiz_question_difficulty;")
    op.execute("UPDATE quiz_question SET difficulty = 'NOVICE' WHERE difficulty = 'REMEMBER';")
    op.execute("UPDATE quiz_question SET difficulty = 'BEGINNER' WHERE difficulty = 'UNDERSTAND';")
    op.execute("UPDATE quiz_question SET difficulty = 'COMPETENT' WHERE difficulty = 'APPLY';")
    op.execute("""
        ALTER TABLE quiz_question ADD CONSTRAINT ck_quiz_question_difficulty 
        CHECK (difficulty IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER', 'REMEMBER', 'UNDERSTAND', 'APPLY'));
    """)

    # 6. Thêm cột skill_report vào quiz_attempt
    with op.batch_alter_table("quiz_attempt") as batch:
        batch.add_column(sa.Column("skill_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("quiz_attempt") as batch:
        batch.drop_column("skill_report")

    op.execute("ALTER TABLE quiz_question DROP CONSTRAINT IF EXISTS ck_quiz_question_difficulty;")
    op.execute("UPDATE quiz_question SET difficulty = 'REMEMBER' WHERE difficulty = 'NOVICE';")
    op.execute("UPDATE quiz_question SET difficulty = 'UNDERSTAND' WHERE difficulty = 'BEGINNER';")
    op.execute("UPDATE quiz_question SET difficulty = 'APPLY' WHERE difficulty IN ('COMPETENT', 'PROFICIENT', 'MASTER');")
    op.execute("""
        ALTER TABLE quiz_question ADD CONSTRAINT ck_quiz_question_difficulty 
        CHECK (difficulty IN ('REMEMBER', 'UNDERSTAND', 'APPLY'));
    """)

    op.drop_index("ix_quiz_question_skill_id", table_name="quiz_question")
    with op.batch_alter_table("quiz_question") as batch:
        batch.drop_constraint("fk_quiz_question_skill_id", type_="foreignkey")
        batch.drop_column("skill_id")

    op.drop_table("skill_level_rubric")
    op.drop_table("skill")
