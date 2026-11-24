"""改写运行的数据表：事实补栏目、运行记录简历底稿，新增改写条目与条目引用事实表。

fact 只追加、触发器拒绝 UPDATE：加列和加约束不改已有行，不会触发；旧事实的栏目为空。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261124_01"
down_revision: str | Sequence[str] | None = "20261123_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECTIONS = "('education', 'project', 'internship', 'skill', 'award', 'other')"


def upgrade() -> None:
    op.add_column("fact", sa.Column("section", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_fact_section", "fact", f"section IS NULL OR section IN {SECTIONS}"
    )
    op.create_unique_constraint(
        op.f("uq_fact_fact_id"), "fact", ["fact_id", "user_id", "fact_set_id"]
    )

    op.add_column("ai_run", sa.Column("base_doc_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_ai_run_base_doc_id_resume_document"),
        "ai_run",
        "resume_document",
        ["base_doc_id", "user_id"],
        ["doc_id", "user_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_ai_run_run_id_user_id_fact_set_id",
        "ai_run",
        ["run_id", "user_id", "fact_set_id"],
    )

    op.create_table(
        "tailored_bullet",
        sa.Column("bullet_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fact_set_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("experience", sa.Text(), nullable=True),
        sa.Column("order_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("check_status", sa.String(16), nullable=False),
        sa.Column(
            "revision_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "issues_json",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("user_decision", sa.String(16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bullet_id", name=op.f("pk_tailored_bullet")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_tailored_bullet_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id", "fact_set_id"],
            ["ai_run.run_id", "ai_run.user_id", "ai_run.fact_set_id"],
            name=op.f("fk_tailored_bullet_run_id_ai_run"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "bullet_id",
            "user_id",
            "fact_set_id",
            name=op.f("uq_tailored_bullet_bullet_id"),
        ),
        sa.CheckConstraint(
            "section IN ('experience', 'skills')", name="ck_tailored_bullet_section"
        ),
        sa.CheckConstraint(
            "(section = 'experience') = (experience IS NOT NULL)",
            name="ck_tailored_bullet_experience",
        ),
        sa.CheckConstraint(
            "check_status IN ('passed', 'fallback', 'pending')",
            name="ck_tailored_bullet_check_status",
        ),
        sa.CheckConstraint(
            "user_decision IS NULL OR user_decision IN ('accepted', 'rejected')",
            name="ck_tailored_bullet_user_decision",
        ),
        sa.CheckConstraint("order_no >= 0", name="ck_tailored_bullet_order_no"),
        sa.CheckConstraint(
            "revision_count BETWEEN 0 AND 2", name="ck_tailored_bullet_revision_count"
        ),
    )
    op.create_index(
        "ix_tailored_bullet_run_id_order_no", "tailored_bullet", ["run_id", "order_no"]
    )

    op.create_table(
        "bullet_fact",
        sa.Column("bullet_id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fact_set_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bullet_id", "fact_id", name=op.f("pk_bullet_fact")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_bullet_fact_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bullet_id", "user_id", "fact_set_id"],
            [
                "tailored_bullet.bullet_id",
                "tailored_bullet.user_id",
                "tailored_bullet.fact_set_id",
            ],
            name=op.f("fk_bullet_fact_bullet_id_tailored_bullet"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id", "user_id", "fact_set_id"],
            ["fact.fact_id", "fact.user_id", "fact.fact_set_id"],
            name=op.f("fk_bullet_fact_fact_id_fact"),
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("bullet_fact")
    op.drop_index("ix_tailored_bullet_run_id_order_no", "tailored_bullet")
    op.drop_table("tailored_bullet")
    op.drop_constraint("uq_ai_run_run_id_user_id_fact_set_id", "ai_run", type_="unique")
    op.drop_constraint(
        op.f("fk_ai_run_base_doc_id_resume_document"), "ai_run", type_="foreignkey"
    )
    op.drop_column("ai_run", "base_doc_id")
    op.drop_constraint(op.f("uq_fact_fact_id"), "fact", type_="unique")
    op.drop_constraint("ck_fact_section", "fact", type_="check")
    op.drop_column("fact", "section")
