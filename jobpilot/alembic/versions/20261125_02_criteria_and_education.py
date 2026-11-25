"""新增求职条件与教育表（只追加）及草稿中的对应字段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261125_02"
down_revision: str | Sequence[str] | None = "20261125_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _provenance_columns() -> list[sa.Column]:
    return [
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_doc_id", sa.Uuid(), nullable=True),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column("span_start", sa.Integer(), nullable=True),
        sa.Column("span_end", sa.Integer(), nullable=True),
        sa.Column(
            "quote_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _provenance_constraints(table_name: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(
            ["fact_set_id", "user_id"],
            ["fact_set.fact_set_id", "fact_set.user_id"],
            name=op.f(f"fk_{table_name}_fact_set_id_fact_set"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_doc_id", "user_id"],
            ["resume_document.doc_id", "resume_document.user_id"],
            name=op.f(f"fk_{table_name}_source_doc_id_resume_document"),
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "(source = 'resume' AND source_doc_id IS NOT NULL "
            "AND source_quote IS NOT NULL AND span_start IS NOT NULL "
            "AND span_end IS NOT NULL AND quote_verified) "
            "OR (source = 'user' AND source_doc_id IS NULL "
            "AND source_quote IS NULL AND span_start IS NULL AND span_end IS NULL)",
            name=f"ck_{table_name}_source",
        ),
        sa.CheckConstraint(
            "span_start IS NULL OR (span_start >= 0 AND span_start < span_end)",
            name=f"ck_{table_name}_span",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "fact_set_criterion",
        sa.Column("criterion_id", sa.Uuid(), nullable=False),
        sa.Column("fact_set_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("int_value", sa.Integer(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        *_provenance_columns(),
        sa.PrimaryKeyConstraint("criterion_id", name=op.f("pk_fact_set_criterion")),
        sa.UniqueConstraint(
            "fact_set_id", "field", name=op.f("uq_fact_set_criterion_fact_set_id")
        ),
        *_provenance_constraints("fact_set_criterion"),
        sa.CheckConstraint(
            "field IN ('onsite_days_per_week', 'internship_months', 'earliest_start')",
            name="ck_fact_set_criterion_field",
        ),
        sa.CheckConstraint(
            "(field = 'onsite_days_per_week' "
            "AND int_value BETWEEN 1 AND 7 AND text_value IS NULL) OR "
            "(field = 'internship_months' "
            "AND int_value BETWEEN 1 AND 24 AND text_value IS NULL) OR "
            "(field = 'earliest_start' AND int_value IS NULL "
            "AND text_value IS NOT NULL AND length(btrim(text_value)) > 0)",
            name="ck_fact_set_criterion_value",
        ),
    )
    op.execute("""
        CREATE TRIGGER fact_set_criterion_append_only
        BEFORE UPDATE ON fact_set_criterion
        FOR EACH ROW EXECUTE FUNCTION reject_fact_update()
    """)

    op.create_table(
        "fact_set_education",
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("fact_set_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("education_id", sa.Uuid(), nullable=False),
        sa.Column("school", sa.Text(), nullable=False),
        sa.Column("major", sa.Text(), nullable=True),
        sa.Column("degree", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("start_text", sa.Text(), nullable=True),
        sa.Column("end_text", sa.Text(), nullable=True),
        *_provenance_columns(),
        sa.PrimaryKeyConstraint("record_id", name=op.f("pk_fact_set_education")),
        sa.UniqueConstraint(
            "user_id",
            "fact_set_id",
            "education_id",
            name=op.f("uq_fact_set_education_user_id"),
        ),
        *_provenance_constraints("fact_set_education"),
        sa.CheckConstraint(
            "length(btrim(school)) > 0", name="ck_fact_set_education_school"
        ),
        sa.CheckConstraint(
            "degree IN ('associate', 'bachelor', 'master', 'phd')",
            name="ck_fact_set_education_degree",
        ),
        sa.CheckConstraint(
            "status IN ('enrolled', 'completed')",
            name="ck_fact_set_education_status",
        ),
    )
    op.execute("""
        CREATE TRIGGER fact_set_education_append_only
        BEFORE UPDATE ON fact_set_education
        FOR EACH ROW EXECUTE FUNCTION reject_fact_update()
    """)

    op.add_column(
        "fact_draft",
        sa.Column(
            "criteria_json",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "fact_draft",
        sa.Column(
            "education_json",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("fact_draft", "education_json")
    op.drop_column("fact_draft", "criteria_json")
    op.execute("DROP TRIGGER fact_set_education_append_only ON fact_set_education")
    op.drop_table("fact_set_education")
    op.execute("DROP TRIGGER fact_set_criterion_append_only ON fact_set_criterion")
    op.drop_table("fact_set_criterion")
