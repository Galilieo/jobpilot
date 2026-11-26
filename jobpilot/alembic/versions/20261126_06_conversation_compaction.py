"""为对话增加有覆盖边界的工作摘要和独立压缩运行。"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261126_06"
down_revision = "20261126_05"
branch_labels = None
depends_on = None

_OLD_KINDS = "'parse_resume', 'parse_jd', 'match', 'tailor', 'agent'"
_ACTIVE_COMPACT = "kind = 'compact' AND status IN ('running', 'waiting_confirmation')"
_SUMMARY_BOUNDARY = (
    "(summary_json IS NULL AND summary_through_seq = 0 AND summary_revision = 0) OR "
    "(summary_json IS NOT NULL AND jsonb_typeof(summary_json) = 'object' "
    "AND summary_through_seq > 0 AND summary_revision > 0)"
)


def upgrade() -> None:
    op.add_column("conversation", sa.Column("summary_json", JSONB(none_as_null=True)))
    op.add_column(
        "conversation",
        sa.Column(
            "summary_through_seq", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "conversation",
        sa.Column(
            "summary_revision", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_conversation_summary_boundary", "conversation", _SUMMARY_BOUNDARY
    )
    op.drop_column("conversation", "summary")
    op.drop_constraint("ck_ai_run_kind", "ai_run", type_="check")
    op.create_check_constraint(
        "ck_ai_run_kind", "ai_run", f"kind IN ({_OLD_KINDS}, 'compact')"
    )
    op.create_index(
        "uq_ai_run_active_compact",
        "ai_run",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_COMPACT),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_run_active_compact", table_name="ai_run")
    # 旧版看不懂压缩运行。账本仍按既有外键规则保留已发生的调用。
    op.execute("DELETE FROM ai_run WHERE kind = 'compact'")
    op.drop_constraint("ck_ai_run_kind", "ai_run", type_="check")
    op.create_check_constraint("ck_ai_run_kind", "ai_run", f"kind IN ({_OLD_KINDS})")
    op.drop_constraint(
        "ck_conversation_summary_boundary", "conversation", type_="check"
    )
    op.drop_column("conversation", "summary_revision")
    op.drop_column("conversation", "summary_through_seq")
    op.drop_column("conversation", "summary_json")
    op.add_column("conversation", sa.Column("summary", sa.Text()))
