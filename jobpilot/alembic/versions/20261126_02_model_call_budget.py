"""记录对话共享的预算归属和每次发送前预扣的 token。"""

import sqlalchemy as sa

from alembic import op

revision = "20261126_02"
down_revision = "20261126_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_call", sa.Column("budget_run_id", sa.Uuid()))
    op.add_column("model_call", sa.Column("reserved_tokens", sa.Integer()))
    op.create_foreign_key(
        op.f("fk_model_call_budget_run_id_ai_run"),
        "model_call",
        "ai_run",
        ["budget_run_id"],
        ["run_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_model_call_budget_run_id",
        "model_call",
        ["budget_run_id"],
    )
    op.create_check_constraint(
        op.f("ck_model_call_reserved_tokens"), "model_call", "reserved_tokens > 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_model_call_reserved_tokens"), "model_call", type_="check"
    )
    op.drop_index("ix_model_call_budget_run_id", table_name="model_call")
    op.drop_constraint(
        op.f("fk_model_call_budget_run_id_ai_run"), "model_call", type_="foreignkey"
    )
    op.drop_column("model_call", "reserved_tokens")
    op.drop_column("model_call", "budget_run_id")
