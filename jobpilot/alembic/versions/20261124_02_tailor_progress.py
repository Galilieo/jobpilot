"""改写运行记录停止原因和每块的结果：预算用完时查询接口只读业务表，不读检查点。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261124_02"
down_revision: str | Sequence[str] | None = "20261124_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STOP_REASONS = (
    "('budget_exhausted', 'match_failed', 'rewrite_failed', 'service_restarted')"
)


def upgrade() -> None:
    op.add_column("ai_run", sa.Column("stop_reason", sa.String(32), nullable=True))
    op.add_column("ai_run", sa.Column("plan_json", JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_ai_run_stop_reason",
        "ai_run",
        f"stop_reason IS NULL OR (stop_reason IN {STOP_REASONS} "
        "AND status NOT IN ('running', 'waiting_confirmation'))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_run_stop_reason", "ai_run", type_="check")
    op.drop_column("ai_run", "plan_json")
    op.drop_column("ai_run", "stop_reason")
