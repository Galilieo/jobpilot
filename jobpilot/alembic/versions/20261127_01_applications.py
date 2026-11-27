"""为投递和阶段历史建表，把旧的岗位进度补录为无简历快照的投递。"""

import sqlalchemy as sa
from jobpilot.db.application_schema import (
    CHANNEL_LENGTH,
    SNAPSHOT_CHECK,
    ApplicationStage,
    stage_check,
)
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261127_01"
down_revision = "20261126_06"
branch_labels = None
depends_on = None

_OLD_OPERATIONS = "'parse_resume', 'parse_jd', 'match', 'tailor'"
_OLD_STAGES = "'saved', 'applied', 'interviewing', 'offer', 'rejected', 'archived'"


def _operation_check(operations: str) -> None:
    op.drop_constraint("ck_idempotency_key_operation", "idempotency_key", type_="check")
    op.create_check_constraint(
        "ck_idempotency_key_operation",
        "idempotency_key",
        f"operation IN ({operations})",
    )


def _create_application() -> None:
    op.create_table(
        "application",
        sa.Column("application_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("user_account.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_job_id", sa.Uuid()),
        sa.Column("job_snapshot_json", JSONB(), nullable=False),
        sa.Column("resume_version_id", sa.Uuid()),
        sa.Column("resume_id", sa.Uuid()),
        sa.Column("resume_version", sa.Integer()),
        sa.Column("fact_set_id", sa.Uuid()),
        sa.Column("snapshot_json", JSONB(none_as_null=True)),
        sa.Column("snapshot_html", sa.Text()),
        sa.Column(
            "backfilled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "applied_at",
            sa.Date(),
            server_default=sa.func.current_date(),
            nullable=False,
        ),
        sa.Column("channel", sa.String(CHANNEL_LENGTH)),
        sa.Column(
            "stage",
            sa.String(16),
            server_default=ApplicationStage.APPLIED,
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("action_id", sa.Uuid(), unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("application_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["user_job_id", "user_id"],
            ["user_job.user_job_id", "user_job.user_id"],
            ondelete="SET NULL (user_job_id)",
        ),
        sa.ForeignKeyConstraint(
            ["resume_version_id", "user_id"],
            ["resume_version.resume_version_id", "resume_version.user_id"],
            ondelete="SET NULL (resume_version_id)",
        ),
        sa.ForeignKeyConstraint(
            ["fact_set_id", "user_id"],
            ["fact_set.fact_set_id", "fact_set.user_id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(SNAPSHOT_CHECK, name="ck_application_snapshot"),
        sa.CheckConstraint(stage_check("stage"), name="ck_application_stage"),
        sa.CheckConstraint("revision >= 1", name="ck_application_revision"),
    )
    op.create_index(
        "ix_application_owner_job_date",
        "application",
        ["user_id", "user_job_id", "applied_at", "created_at"],
    )


def _create_stage_events() -> None:
    op.create_table(
        "application_stage_event",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage", sa.String(16)),
        sa.Column("to_stage", sa.String(16), nullable=False),
        sa.Column(
            "occurred_at",
            sa.Date(),
            server_default=sa.func.current_date(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id", "user_id"],
            ["application.application_id", "application.user_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            stage_check("from_stage"), name="ck_application_event_from_stage"
        ),
        sa.CheckConstraint(
            stage_check("to_stage"), name="ck_application_event_to_stage"
        ),
    )
    op.create_index(
        "ix_application_event_owner_application",
        "application_stage_event",
        ["user_id", "application_id", "created_at"],
    )


def _backfill() -> None:
    op.execute("""
        WITH prior_jobs AS (
            SELECT j.*,
                   COALESCE(p.parsed_json->'basic_fields', '{}'::jsonb) ||
                   COALESCE(j.field_overrides_json->'basic_fields', '{}'::jsonb)
                       AS fields
            FROM user_job j
            JOIN job_posting p ON (p.posting_id, p.owner_id) = (j.posting_id, j.user_id)
            WHERE j.stage IN ('applied', 'interviewing', 'offer', 'rejected')
        ), copied AS (
            INSERT INTO application (
                application_id, user_id, user_job_id, job_snapshot_json,
                applied_at, stage, backfilled
            )
            SELECT gen_random_uuid(), user_id, user_job_id,
                   jsonb_build_object('title', fields->'title',
                       'company', fields->'company',
                       'location', fields->'location', 'salary', fields->'salary',
                       'job_url', fields->'job_url'),
                   updated_at::date, stage, true
            FROM prior_jobs
            RETURNING application_id, user_id, stage, applied_at
        )
        INSERT INTO application_stage_event (
            event_id, application_id, user_id, to_stage, occurred_at
        )
        SELECT gen_random_uuid(), application_id, user_id, stage, applied_at FROM copied
    """)
    op.execute("UPDATE user_job SET archived_at = updated_at WHERE stage = 'archived'")


def upgrade() -> None:
    op.add_column("user_job", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        "uq_resume_version_owner", "resume_version", ["resume_version_id", "user_id"]
    )
    _create_application()
    _create_stage_events()
    _backfill()
    op.drop_column("user_job", "stage")
    _operation_check(f"{_OLD_OPERATIONS}, 'record_application'")


def downgrade() -> None:
    # 旧结构只能表示最近一次投递的进度，多次投递、快照及阶段历史会丢失。
    # 单独填写的投递日期和渠道也会丢失，投递确认卡及等待这些卡片的运行在旧版失效。
    op.add_column(
        "user_job",
        sa.Column("stage", sa.String(16), server_default="saved", nullable=False),
    )
    op.execute("""
        UPDATE user_job j
        SET stage = CASE
            WHEN j.archived_at IS NOT NULL THEN 'archived'
            WHEN latest.stage = 'written_test' THEN 'applied'
            ELSE COALESCE(latest.stage, 'saved')
        END
        FROM (
            SELECT DISTINCT ON (job.user_job_id) job.user_job_id, a.stage
            FROM user_job job LEFT JOIN application a
                ON (a.user_job_id, a.user_id) = (job.user_job_id, job.user_id)
            ORDER BY job.user_job_id, a.applied_at DESC,
                     a.created_at DESC, a.application_id DESC
        ) latest
        WHERE j.user_job_id = latest.user_job_id
    """)
    op.create_check_constraint(
        "ck_user_job_stage", "user_job", f"stage IN ({_OLD_STAGES})"
    )
    op.execute("DELETE FROM idempotency_key WHERE operation = 'record_application'")
    _operation_check(_OLD_OPERATIONS)
    op.drop_table("application_stage_event")
    op.drop_table("application")
    op.drop_constraint("uq_resume_version_owner", "resume_version", type_="unique")
    op.drop_column("user_job", "archived_at")
