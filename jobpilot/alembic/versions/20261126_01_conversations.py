"""增加对话、消息、确认卡和同一对话的活跃运行唯一约束。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261126_01"
down_revision: str | Sequence[str] | None = "20261125_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA jobpilot_checkpoint")
    op.create_table(
        "conversation",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("current_job_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["current_job_id", "user_id"],
            ["user_job.user_job_id", "user_job.user_id"],
            name=op.f("fk_conversation_current_job_id_user_job"),
            ondelete="SET NULL (current_job_id)",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_conversation_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_conversation")),
        sa.UniqueConstraint(
            "conversation_id", "user_id", name=op.f("uq_conversation_conversation_id")
        ),
    )
    op.create_index(
        "ix_conversation_user_id_updated_at",
        "conversation",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.add_column("ai_run", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_index(
        "uq_ai_run_active_conversation",
        "ai_run",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'agent' AND status IN ('running', 'waiting_confirmation')"
        ),
    )
    op.create_unique_constraint(
        "uq_ai_run_conversation_owner",
        "ai_run",
        ["run_id", "conversation_id", "user_id"],
    )
    op.create_foreign_key(
        op.f("fk_ai_run_conversation_id_conversation"),
        "ai_run",
        "conversation",
        ["conversation_id", "user_id"],
        ["conversation_id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "agent_action",
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("args_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column(
            "decision_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("undo_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', "
            "'executed', 'failed', 'undone')",
            name="ck_agent_action_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "conversation_id", "user_id"],
            ["ai_run.run_id", "ai_run.conversation_id", "ai_run.user_id"],
            name=op.f("fk_agent_action_run_id_ai_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("action_id", name=op.f("pk_agent_action")),
        sa.UniqueConstraint(
            "run_id", "tool_call_id", name="uq_agent_action_run_tool_call"
        ),
    )
    op.create_table(
        "message",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "attachments_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "steps_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')", name="ck_message_role"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "conversation_id", "user_id"],
            ["ai_run.run_id", "ai_run.conversation_id", "ai_run.user_id"],
            name=op.f("fk_message_run_id_ai_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name=op.f("pk_message")),
        sa.UniqueConstraint("run_id", "event_key", name="uq_message_run_event"),
    )
    op.create_index(
        "ix_message_conversation_id_seq",
        "message",
        ["conversation_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_conversation_id_seq", table_name="message")
    op.drop_table("message")
    op.drop_table("agent_action")
    op.drop_constraint(
        op.f("fk_ai_run_conversation_id_conversation"), "ai_run", type_="foreignkey"
    )
    op.drop_constraint("uq_ai_run_conversation_owner", "ai_run", type_="unique")
    op.drop_index(
        "uq_ai_run_active_conversation",
        table_name="ai_run",
        postgresql_where=sa.text(
            "kind = 'agent' AND status IN ('running', 'waiting_confirmation')"
        ),
    )
    op.drop_column("ai_run", "conversation_id")
    op.drop_index("ix_conversation_user_id_updated_at", table_name="conversation")
    op.drop_table("conversation")
    op.execute("DROP SCHEMA jobpilot_checkpoint CASCADE")
