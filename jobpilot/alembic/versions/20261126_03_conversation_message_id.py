"""把客户端消息 ID 绑定到对话，供网络重试定位原运行。"""

import sqlalchemy as sa

from alembic import op

revision = "20261126_03"
down_revision = "20261126_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message", sa.Column("client_message_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_message_client_id", "message", ["conversation_id", "client_message_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_message_client_id", "message", type_="unique")
    op.drop_column("message", "client_message_id")
