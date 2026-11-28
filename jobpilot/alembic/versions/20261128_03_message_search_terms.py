"""为消息块增加中文全文检索词；存量分块由补算脚本更新。"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision = "20261128_03"
down_revision = "20261128_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_chunk", sa.Column("search_terms", TSVECTOR()))


def downgrade() -> None:
    op.drop_column("message_chunk", "search_terms")
