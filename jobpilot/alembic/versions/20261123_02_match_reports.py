"""运行补齐岗位快照列，新增匹配报告表 match_report 并用组合外键绑定同用户运行。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261123_02"
down_revision: str | Sequence[str] | None = "20261123_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, JSONB(), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.add_column("ai_run", sa.Column("job_snapshot_json", JSONB(), nullable=True))
    op.create_table(
        "match_report",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        _jsonb("items_json", "'[]'::jsonb"),
        _jsonb("main_gaps", "'[]'::jsonb"),
        _created_at(),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_match_report")),
        sa.UniqueConstraint("run_id", name=op.f("uq_match_report_run_id")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_match_report_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_run.run_id", "ai_run.user_id"],
            name=op.f("fk_match_report_run_id_ai_run"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_match_report_user_id_created_at", "match_report", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_match_report_user_id_created_at", "match_report")
    op.drop_table("match_report")
    op.drop_column("ai_run", "job_snapshot_json")
