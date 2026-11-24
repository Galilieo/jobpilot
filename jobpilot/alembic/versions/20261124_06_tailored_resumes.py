"""定制简历与版本：一次成功的改写一份简历，每次组装和保存编辑新增一个版本。

两张表互相引用（简历的当前版本指针 ↔ 版本所属的简历），先建两张表，再加指针的外键；
两个外键都在提交时检查，同一事务里先写简历、再写版本也不会报错。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261124_06"
down_revision: str | Sequence[str] | None = "20261124_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "tailored_resume",
        sa.Column("resume_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_run.run_id", "ai_run.user_id"],
            name=op.f("fk_tailored_resume_run_id_ai_run"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_tailored_resume_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("resume_id", name=op.f("pk_tailored_resume")),
        sa.UniqueConstraint(
            "resume_id", "user_id", name=op.f("uq_tailored_resume_resume_id")
        ),
        sa.UniqueConstraint("run_id", name=op.f("uq_tailored_resume_run_id")),
    )
    op.create_table(
        "resume_version",
        sa.Column("resume_version_id", sa.Uuid(), nullable=False),
        sa.Column("resume_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_json", JSONB(), nullable=False),
        _created_at(),
        sa.CheckConstraint("version >= 1", name="ck_resume_version_version"),
        sa.ForeignKeyConstraint(
            ["resume_id", "user_id"],
            ["tailored_resume.resume_id", "tailored_resume.user_id"],
            name=op.f("fk_resume_version_resume_id_tailored_resume"),
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_resume_version_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("resume_version_id", name=op.f("pk_resume_version")),
        sa.UniqueConstraint(
            "resume_version_id",
            "resume_id",
            name=op.f("uq_resume_version_resume_version_id"),
        ),
        sa.UniqueConstraint(
            "user_id", "resume_id", "version", name=op.f("uq_resume_version_user_id")
        ),
    )
    op.create_foreign_key(
        op.f("fk_tailored_resume_current_version_id_resume_version"),
        "tailored_resume",
        "resume_version",
        ["current_version_id", "resume_id"],
        ["resume_version_id", "resume_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_tailored_resume_current_version_id_resume_version"),
        "tailored_resume",
        type_="foreignkey",
    )
    op.drop_table("resume_version")
    op.drop_table("tailored_resume")
