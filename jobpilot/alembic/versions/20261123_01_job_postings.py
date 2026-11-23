"""创建岗位原文与个人岗位表，运行关联岗位；V1 岗位全部私有，同一用户内按正文去重。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261123_01"
down_revision: str | Sequence[str] | None = "20261121_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, JSONB(), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        "job_posting",
        sa.Column("posting_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column(
            "visibility", sa.String(16), server_default="private", nullable=False
        ),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parsed_json", JSONB(), nullable=False),
        _jsonb("keywords", "'[]'::jsonb"),
        _jsonb("injection_flags", "'[]'::jsonb"),
        _created_at(),
        sa.PrimaryKeyConstraint("posting_id", name=op.f("pk_job_posting")),
        sa.UniqueConstraint(
            "owner_id", "content_hash", name=op.f("uq_job_posting_owner_id")
        ),
        sa.UniqueConstraint(
            "posting_id", "owner_id", name=op.f("uq_job_posting_posting_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user_account.user_id"],
            name=op.f("fk_job_posting_owner_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'public')", name="ck_job_visibility"
        ),
        sa.CheckConstraint(
            "visibility = 'public' OR owner_id IS NOT NULL", name="ck_job_owner"
        ),
    )

    op.create_table(
        "user_job",
        sa.Column("user_job_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(16), server_default="saved", nullable=False),
        _jsonb("field_overrides_json", "'{}'::jsonb"),
        _jsonb("extra_fields_json", "'{}'::jsonb"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        _created_at(),
        sa.PrimaryKeyConstraint("user_job_id", name=op.f("pk_user_job")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_user_job_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["posting_id", "user_id"],
            ["job_posting.posting_id", "job_posting.owner_id"],
            name=op.f("fk_user_job_posting_id_job_posting"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "posting_id", name=op.f("uq_user_job_user_id")),
        sa.UniqueConstraint(
            "user_job_id", "user_id", name=op.f("uq_user_job_user_job_id")
        ),
        sa.CheckConstraint(
            "stage IN ('saved', 'applied', 'interviewing', 'offer', 'rejected', "
            "'archived')",
            name="ck_user_job_stage",
        ),
    )
    op.create_index(
        "ix_user_job_user_id_updated_at", "user_job", ["user_id", "updated_at"]
    )

    op.add_column("ai_run", sa.Column("user_job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_ai_run_user_job_id_user_job"),
        "ai_run",
        "user_job",
        ["user_job_id", "user_id"],
        ["user_job_id", "user_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_ai_run_user_job_id_user_job"), "ai_run", type_="foreignkey"
    )
    op.drop_column("ai_run", "user_job_id")
    op.drop_index("ix_user_job_user_id_updated_at", "user_job")
    op.drop_table("user_job")
    op.drop_table("job_posting")
