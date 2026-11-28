"""为跨对话搜索创建可按消息级联删除的分块表。"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "20261128_02"
down_revision = "20261128_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_message_chunk_parent",
        "message",
        ["message_id", "conversation_id", "user_id"],
    )
    op.create_table(
        "message_chunk",
        sa.Column("chunk_id", sa.Uuid(), primary_key=True),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column("embedding_model", sa.String(200)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "conversation_id", "user_id"],
            ["message.message_id", "message.conversation_id", "message.user_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("message_id", "chunk_no", name="uq_message_chunk_number"),
        sa.CheckConstraint(
            "chunk_no >= 0 AND span_start >= 0 AND span_end > span_start "
            "AND span_end - span_start = char_length(content)",
            name="ck_message_chunk_span",
        ),
        sa.CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="ck_message_chunk_embedding_model",
        ),
    )
    op.create_index("ix_message_chunk_user_id", "message_chunk", ["user_id"])


def downgrade() -> None:
    op.drop_table("message_chunk")
    op.drop_constraint("uq_message_chunk_parent", "message", type_="unique")
