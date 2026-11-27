"""保留已删除运行的账号账本归属，彻底删除账号时统一匿名化。"""

from alembic import op

revision = "20261127_03"
down_revision = "20261127_02"
branch_labels = None
depends_on = None

_TABLE = "model_call"
_RUN_FK = "fk_model_call_run_id_ai_run"
_OWNER_FK = "fk_model_call_user_id_user_account"
_OWNERSHIP_CHECK = "ck_model_call_run_ownership"


def _drop_run_constraints() -> None:
    op.drop_constraint(_RUN_FK, _TABLE, type_="foreignkey")
    op.drop_constraint(_OWNERSHIP_CHECK, _TABLE, type_="check")


def _create_run_constraints(on_delete: str, check: str) -> None:
    op.create_foreign_key(
        _RUN_FK,
        _TABLE,
        "ai_run",
        ["run_id", "user_id"],
        ["run_id", "user_id"],
        ondelete=on_delete,
    )
    op.create_check_constraint(_OWNERSHIP_CHECK, _TABLE, check)


def upgrade() -> None:
    _drop_run_constraints()
    _create_run_constraints(
        "SET NULL (run_id)", "run_id IS NULL OR user_id IS NOT NULL"
    )
    op.create_foreign_key(_OWNER_FK, _TABLE, "user_account", ["user_id"], ["user_id"])
    # 在其他外键级联前更新两列，避免中途违反运行归属约束。
    op.execute("""
        CREATE FUNCTION anonymize_deleted_account_model_calls() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE model_call SET run_id = NULL, user_id = NULL, budget_run_id = NULL
            WHERE user_id = OLD.user_id;
            RETURN OLD;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER anonymize_deleted_account_model_calls
        BEFORE DELETE ON user_account FOR EACH ROW
        EXECUTE FUNCTION anonymize_deleted_account_model_calls()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER anonymize_deleted_account_model_calls ON user_account")
    op.execute("DROP FUNCTION anonymize_deleted_account_model_calls()")
    op.drop_constraint(_OWNER_FK, _TABLE, type_="foreignkey")
    _drop_run_constraints()
    # 旧版不允许没有运行却保留账号关联。
    op.execute(
        "UPDATE model_call SET user_id = NULL "
        "WHERE run_id IS NULL AND user_id IS NOT NULL"
    )
    _create_run_constraints("SET NULL", "(run_id IS NULL) = (user_id IS NULL)")
