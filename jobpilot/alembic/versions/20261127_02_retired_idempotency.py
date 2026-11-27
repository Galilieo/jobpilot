"""允许保留已删除业务结果的幂等键，禁止继续重放响应。"""

from alembic import op

revision = "20261127_02"
down_revision = "20261127_01"
branch_labels = None
depends_on = None

_TABLE = "idempotency_key"
_CONSTRAINT = "ck_idempotency_key_status"


def _replace_status_check(statuses: str) -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, f"status IN ({statuses})")


def upgrade() -> None:
    # 响应约束已要求所有非 succeeded 状态不带响应。
    _replace_status_check("'processing', 'succeeded', 'failed', 'retired'")


def downgrade() -> None:
    # 旧版无法识别退役键，降级会失去对应的重试保护。
    op.execute("DELETE FROM idempotency_key WHERE status = 'retired'")
    _replace_status_check("'processing', 'succeeded', 'failed'")
