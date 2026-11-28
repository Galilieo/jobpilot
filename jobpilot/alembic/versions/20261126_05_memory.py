"""创建版本化的记忆档案和改写运行的写作偏好快照列。"""

import sqlalchemy as sa
from jobpilot.db.legacy_memory_schema import (
    MEMORY_KEY_CHECK,
    MEMORY_KEY_LENGTH,
    MEMORY_REVISION_CHECK,
    MEMORY_SOURCE_CHECK,
    MEMORY_VALUE_CHECK,
)
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261126_05"
down_revision = "20261126_04"
branch_labels = None
depends_on = None


def create_legacy_memory() -> None:
    op.create_table(
        "memory",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("user_account.user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(MEMORY_KEY_LENGTH), primary_key=True),
        sa.Column("value_json", JSONB(none_as_null=True)),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("source", JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(MEMORY_KEY_CHECK, name="ck_memory_key"),
        sa.CheckConstraint(MEMORY_REVISION_CHECK, name="ck_memory_revision"),
        sa.CheckConstraint(MEMORY_VALUE_CHECK, name="ck_memory_value"),
        sa.CheckConstraint(MEMORY_SOURCE_CHECK, name="ck_memory_source"),
    )


def upgrade() -> None:
    create_legacy_memory()
    op.add_column("ai_run", sa.Column("writing_preferences_json", JSONB()))


def downgrade() -> None:
    # 写作偏好快照和记忆会丢失，记忆确认卡及等待这些卡片的运行在旧版失效。
    op.drop_column("ai_run", "writing_preferences_json")
    op.drop_table("memory")
