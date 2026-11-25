"""待删除账号不能新增运行，覆盖后台改写内部派生的匹配运行。"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261125_01"
down_revision: str | Sequence[str] | None = "20261124_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 同一账号的多个运行可以并行建；删号必须等已开始的插入提交，再取消它们。
    op.execute("""
        CREATE FUNCTION ensure_run_account_active() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE deletion_time timestamptz;
        BEGIN
            SELECT deletion_requested_at INTO deletion_time
            FROM user_account WHERE user_id = NEW.user_id FOR SHARE;
            IF deletion_time IS NOT NULL THEN
                RAISE check_violation USING
                    MESSAGE = 'Account is pending deletion',
                    CONSTRAINT = 'ck_ai_run_account_active';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_ai_run_account_active
        BEFORE INSERT ON ai_run
        FOR EACH ROW EXECUTE FUNCTION ensure_run_account_active()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_ai_run_account_active ON ai_run")
    op.execute("DROP FUNCTION ensure_run_account_active()")
