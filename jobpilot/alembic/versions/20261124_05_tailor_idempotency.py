"""启动改写也用幂等键：操作类型加 tailor，成功响应允许 202（运行已创建、后台执行）。"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261124_05"
down_revision: str | Sequence[str] | None = "20261124_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESPONSE = (
    "(status = 'succeeded' AND response_status IN {codes} "
    "AND response_status IS NOT NULL AND response_body IS NOT NULL "
    "AND result_id IS NOT NULL) OR "
    "(status <> 'succeeded' AND response_status IS NULL "
    "AND response_body IS NULL)"
)


def _replace(operations: str, codes: str) -> None:
    op.drop_constraint("ck_idempotency_key_operation", "idempotency_key", "check")
    op.drop_constraint("ck_idempotency_key_response", "idempotency_key", "check")
    op.create_check_constraint(
        "ck_idempotency_key_operation", "idempotency_key", f"operation IN {operations}"
    )
    op.create_check_constraint(
        "ck_idempotency_key_response", "idempotency_key", RESPONSE.format(codes=codes)
    )


def upgrade() -> None:
    _replace("('parse_resume', 'parse_jd', 'match', 'tailor')", "(200, 201, 202)")


def downgrade() -> None:
    # 旧约束容不下改写的键；它们只用于重放，删掉后客户端重送会新建运行。
    op.execute("DELETE FROM idempotency_key WHERE operation = 'tailor'")
    _replace("('parse_resume', 'parse_jd', 'match')", "(200, 201)")
