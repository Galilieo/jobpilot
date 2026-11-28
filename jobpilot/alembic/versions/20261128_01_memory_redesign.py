"""将固定记忆栏目迁入条目和写作偏好，并为对话与求职条件扩充字段。"""

from importlib import import_module

import sqlalchemy as sa
from jobpilot.db.memory_schema import (
    MEMORY_ACTIVE_WHERE,
    MEMORY_CATEGORY_CHECK,
    MEMORY_CONTENT_CHARS,
    MEMORY_CONTENT_CHECK,
    MEMORY_NORMALIZED_CONTENT,
    MEMORY_REVISION_CHECK,
    MEMORY_SOURCE_CHECK,
    MEMORY_UNIQUE_INDEX,
    WritingField,
    writing_choice_check,
)
from jobpilot.db.models import CRITERION_FIELD_CHECK, CRITERION_VALUE_CHECK
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261128_01"
down_revision = "20261127_03"
branch_labels = None
depends_on = None

_OLD_FIELDS = "field IN ('onsite_days_per_week', 'internship_months', 'earliest_start')"
_OLD_VALUES = """
    (field = 'onsite_days_per_week' AND int_value BETWEEN 1 AND 7
        AND text_value IS NULL) OR
    (field = 'internship_months' AND int_value BETWEEN 1 AND 24
        AND text_value IS NULL) OR
    (field = 'earliest_start' AND int_value IS NULL AND length(btrim(text_value)) > 0
        AND text_value IS NOT NULL)
"""


def _replace_criterion_checks(fields: str, values: str) -> None:
    for name, expression in (("field", fields), ("value", values)):
        constraint = f"ck_fact_set_criterion_{name}"
        op.drop_constraint(constraint, "fact_set_criterion", type_="check")
        op.create_check_constraint(
            constraint, "fact_set_criterion", sa.text(expression)
        )


def upgrade() -> None:
    op.rename_table("memory", "memory_legacy")
    op.execute(
        "ALTER TABLE memory_legacy RENAME CONSTRAINT pk_memory TO pk_memory_legacy"
    )
    _create_writing_preference()
    _create_memory()
    _migrate_writing()
    _migrate_memories()
    op.create_index(
        MEMORY_UNIQUE_INDEX,
        "memory",
        ["user_id", "category", sa.text(MEMORY_NORMALIZED_CONTENT)],
        unique=True,
        postgresql_where=sa.text(MEMORY_ACTIVE_WHERE),
    )
    op.add_column("fact_set_criterion", sa.Column("list_value", JSONB()))
    _replace_criterion_checks(CRITERION_FIELD_CHECK, CRITERION_VALUE_CHECK)
    op.add_column("conversation", sa.Column("memory_snapshot_json", JSONB()))
    op.drop_table("memory_legacy")


def _user_column(*, primary_key: bool = False) -> sa.Column:
    return sa.Column(
        "user_id",
        sa.Uuid(),
        sa.ForeignKey("user_account.user_id", ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _create_writing_preference() -> None:
    op.create_table(
        "writing_preference",
        _user_column(primary_key=True),
        *(sa.Column(field, sa.Text()) for field in WritingField),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("source", JSONB(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            MEMORY_REVISION_CHECK, name="ck_writing_preference_revision"
        ),
        sa.CheckConstraint(MEMORY_SOURCE_CHECK, name="ck_writing_preference_source"),
        *(
            sa.CheckConstraint(
                writing_choice_check(field), name=f"ck_writing_preference_{field}"
            )
            for field in WritingField
        ),
    )


def _create_memory() -> None:
    op.create_table(
        "memory",
        sa.Column("memory_id", sa.Uuid(), primary_key=True),
        _user_column(),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", JSONB(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(MEMORY_CATEGORY_CHECK, name="ck_memory_category"),
        sa.CheckConstraint(MEMORY_CONTENT_CHECK, name="ck_memory_content"),
        sa.CheckConstraint(MEMORY_REVISION_CHECK, name="ck_memory_revision"),
        sa.CheckConstraint(MEMORY_SOURCE_CHECK, name="ck_memory_source"),
    )


def _migrate_writing() -> None:
    op.execute("""
        INSERT INTO writing_preference
            (user_id, tone, bullet_length, tech_terms, source, updated_at)
        SELECT user_id,
            max(value_json #>> '{}') FILTER (WHERE key = 'tone'),
            max(value_json #>> '{}') FILTER (WHERE key = 'bullet_length'),
            max(value_json #>> '{}') FILTER (WHERE key = 'tech_terms'),
            (array_agg(source ORDER BY updated_at DESC, key))[1], max(updated_at)
        FROM memory_legacy
        WHERE deleted_at IS NULL AND key IN ('tone', 'bullet_length', 'tech_terms')
        GROUP BY user_id
    """)


def _migrate_memories() -> None:
    # 在 200 字内找最后一个句末。单句太长时按字符切，后续内容继续保留。
    op.execute(rf"""
        WITH RECURSIVE active AS (SELECT * FROM memory_legacy WHERE deleted_at IS NULL),
        notes AS (
            SELECT user_id, key, value_json, revision, deleted_at, source, updated_at,
                created_at, 0 AS position, ''::text AS chunk,
                value_json #>> '{{}}' AS rest
            FROM active a WHERE key = 'job_notes'
            UNION ALL
            SELECT user_id, key, value_json, revision, deleted_at, source, updated_at,
                created_at, position + 1, left(rest, cut.size),
                substr(rest, cut.size + 1)
            FROM notes
            CROSS JOIN LATERAL (
                SELECT CASE
                    WHEN length(rest) <= {MEMORY_CONTENT_CHARS} THEN length(rest)
                    ELSE coalesce(
                        length(substring(
                            left(rest, {MEMORY_CONTENT_CHARS}) FROM '^.*[。！？!?；;\n]'
                        )),
                        {MEMORY_CONTENT_CHARS}
                    )
                END AS size
            ) cut
            WHERE rest <> ''
        ),
        entries AS (
            SELECT user_id, key, value_json, revision, deleted_at, source,
                updated_at, created_at,
                '目标方向：' || (value_json #>> '{{}}') AS content
            FROM active a WHERE key = 'target_direction'
            UNION ALL
            SELECT user_id, key, value_json, revision, deleted_at, source,
                updated_at, created_at,
                '意向城市：' || cities.content
            FROM active a CROSS JOIN LATERAL (
                SELECT string_agg(city, '、' ORDER BY pos) AS content
                FROM jsonb_array_elements_text(
                    CASE WHEN key = 'cities' THEN value_json ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS item(city, pos)
            ) cities WHERE key = 'cities'
            UNION ALL
            SELECT user_id, key, value_json, revision, deleted_at, source,
                updated_at, created_at,
                '不考虑：' || btrim(item)
            FROM active a CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN key = 'excluded_types' THEN value_json ELSE '[]'::jsonb END
            ) item WHERE key = 'excluded_types'
            UNION ALL
            SELECT user_id, key, value_json, revision, deleted_at, source,
                updated_at, created_at + position * interval '1 microsecond', chunk
            FROM notes WHERE position > 0
        ), normalized AS (
            SELECT user_id, key, {MEMORY_NORMALIZED_CONTENT} AS content,
                source, created_at, updated_at
            FROM entries
        )
        INSERT INTO memory
            (memory_id, user_id, category, content, source, created_at, updated_at)
        SELECT DISTINCT ON (user_id, content) gen_random_uuid(), user_id,
            'job_pref', content, source, created_at, updated_at
        FROM normalized WHERE length(content) > 0
        ORDER BY user_id, content, updated_at DESC, key
    """)


def downgrade() -> None:
    """只恢复可映射的旧栏目。自由记忆、快照和新求职条件在降级时丢失。"""
    op.drop_column("conversation", "memory_snapshot_json")
    op.execute(
        "DELETE FROM fact_set_criterion "
        "WHERE field IN ('target_direction', 'preferred_cities')"
    )
    op.execute("""
        UPDATE fact_draft SET criteria_json = (
            SELECT coalesce(jsonb_agg(item ORDER BY pos), '[]'::jsonb)
            FROM jsonb_array_elements(criteria_json) WITH ORDINALITY AS entry(item, pos)
            WHERE item->>'field' NOT IN ('target_direction', 'preferred_cities')
        )
    """)
    _replace_criterion_checks(_OLD_FIELDS, _OLD_VALUES)
    op.drop_column("fact_set_criterion", "list_value")
    op.rename_table("memory", "memory_entries")
    op.execute(
        "ALTER TABLE memory_entries RENAME CONSTRAINT pk_memory TO pk_memory_entries"
    )
    legacy = import_module("jobpilot.alembic.versions.20261126_05_memory")
    legacy.create_legacy_memory()
    _restore_legacy()
    op.drop_table("memory_entries")
    op.drop_table("writing_preference")


def _restore_legacy() -> None:
    op.execute("""
        INSERT INTO memory (user_id, key, value_json, revision, source, updated_at)
        SELECT user_id, field, to_jsonb(value), 1, source, updated_at
        FROM writing_preference CROSS JOIN LATERAL (
            VALUES ('tone', tone), ('bullet_length', bullet_length),
                ('tech_terms', tech_terms)
        ) choice(field, value) WHERE value IS NOT NULL
    """)
    op.execute("""
        WITH eligible AS (
            SELECT *, CASE
                WHEN content LIKE '目标方向：%' THEN 'target_direction'
                WHEN content LIKE '意向城市：%' THEN 'cities'
                WHEN content LIKE '不考虑：%' THEN 'excluded_types'
                ELSE 'job_notes'
            END AS field,
            substr(content, 6) AS direction,
            string_to_array(substr(content, 6), '、') AS cities,
            substr(content, 5) AS excluded
            FROM memory_entries WHERE deleted_at IS NULL AND category = 'job_pref'
        ), restored AS (
            SELECT user_id, field,
                (array_agg(source ORDER BY updated_at DESC, memory_id))[1] AS source,
                max(updated_at) AS updated_at,
                CASE field
                    WHEN 'target_direction' THEN to_jsonb(
                        (array_agg(direction ORDER BY updated_at DESC, memory_id)
                            FILTER (WHERE length(direction) BETWEEN 1 AND 100))[1])
                    WHEN 'cities' THEN to_jsonb(
                        (array_agg(cities::text ORDER BY updated_at DESC, memory_id)
                            FILTER (WHERE cardinality(cities) BETWEEN 1 AND 5
                                AND NOT EXISTS (
                                    SELECT 1 FROM unnest(cities) city
                                    WHERE length(city) NOT BETWEEN 1 AND 20
                                )))[1]::text[])
                    WHEN 'excluded_types' THEN to_jsonb(
                        (array_agg(excluded ORDER BY created_at, memory_id)
                            FILTER (WHERE length(excluded) BETWEEN 1 AND 30))[1:5])
                    ELSE to_jsonb(
                        left(string_agg(
                            content, E'\\n' ORDER BY created_at, memory_id
                        ), 300)
                    )
                END AS value_json
            FROM eligible GROUP BY user_id, field
        )
        INSERT INTO memory (user_id, key, value_json, revision, source, updated_at)
        SELECT user_id, field, value_json, 1, source, updated_at
        FROM restored WHERE value_json IS NOT NULL
    """)
