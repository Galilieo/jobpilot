"""为对话岗位绑定加修订号，包含外键触发的解绑。"""

import sqlalchemy as sa

from alembic import op

revision = "20261126_04"
down_revision = "20261126_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column(
            "job_binding_revision", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    op.execute("""
        CREATE FUNCTION advance_conversation_job_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.current_job_id IS DISTINCT FROM OLD.current_job_id
                AND NEW.job_binding_revision = OLD.job_binding_revision THEN
                NEW.job_binding_revision := OLD.job_binding_revision + 1;
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_conversation_job_binding
        BEFORE UPDATE OF current_job_id ON conversation
        FOR EACH ROW EXECUTE FUNCTION advance_conversation_job_binding()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_conversation_job_binding ON conversation")
    op.execute("DROP FUNCTION advance_conversation_job_binding()")
    op.drop_column("conversation", "job_binding_revision")
