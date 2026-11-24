"""登记付费 POST 的幂等键及响应快照。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261124_04"
down_revision: str | Sequence[str] | None = "20261124_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_key",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_id", sa.Uuid(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "operation", "key", name=op.f("pk_idempotency_key")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_idempotency_key_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_run.run_id", "ai_run.user_id"],
            name=op.f("fk_idempotency_key_run_id_ai_run"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "operation IN ('parse_resume', 'parse_jd', 'match')",
            name="ck_idempotency_key_operation",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')",
            name="ck_idempotency_key_status",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND response_status IN (200, 201) "
            "AND response_status IS NOT NULL AND response_body IS NOT NULL "
            "AND result_id IS NOT NULL) OR "
            "(status <> 'succeeded' AND response_status IS NULL "
            "AND response_body IS NULL)",
            name="ck_idempotency_key_response",
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_key")
