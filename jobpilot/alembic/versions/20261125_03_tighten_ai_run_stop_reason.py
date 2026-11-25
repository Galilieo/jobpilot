"""收紧 ai_run 的停止原因与终态约束：必须按固定配对写入。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261125_03"
down_revision: str | Sequence[str] | None = "20261125_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 固定当时 jobpilot/db/models.py:STOP_REASON_STATUS 的配对，不导入会随代码变化的对象。
NEW_CHECK_SQL = (
    "(stop_reason IS NULL AND status IN ('running', 'waiting_confirmation', "
    "'succeeded', 'failed', 'cancelled', 'budget_exhausted')) OR "
    "((stop_reason = 'budget_exhausted' AND status = 'budget_exhausted') OR "
    "(stop_reason = 'match_failed' AND status = 'failed') OR "
    "(stop_reason = 'rewrite_failed' AND status = 'failed') OR "
    "(stop_reason = 'service_restarted' AND status = 'failed'))"
)

# 20261124_02 的旧约束，供 downgrade 恢复。
OLD_CHECK_SQL = (
    "stop_reason IS NULL OR (stop_reason IN "
    "('budget_exhausted', 'match_failed', 'rewrite_failed', 'service_restarted') "
    "AND status NOT IN ('running', 'waiting_confirmation'))"
)


def upgrade() -> None:
    bind = op.get_bind()
    invalid_count = bind.execute(
        sa.text(f"SELECT count(*) FROM ai_run WHERE NOT ({NEW_CHECK_SQL})")
    ).scalar_one()
    if invalid_count:
        raise RuntimeError(
            f"ai_run 中有 {invalid_count} 行违反新的 stop_reason 与 status 配对约束"
        )
    op.drop_constraint("ck_ai_run_stop_reason", "ai_run", type_="check")
    op.create_check_constraint("ck_ai_run_stop_reason", "ai_run", NEW_CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint("ck_ai_run_stop_reason", "ai_run", type_="check")
    op.create_check_constraint("ck_ai_run_stop_reason", "ai_run", OLD_CHECK_SQL)
