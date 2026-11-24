"""为事实草稿增加修订号，已有草稿从 0 开始。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261124_03"
down_revision: str | Sequence[str] | None = "20261124_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fact_draft",
        sa.Column(
            "revision", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("fact_draft", "revision")
