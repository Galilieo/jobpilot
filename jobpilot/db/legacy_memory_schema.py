"""旧版记忆栏目约束，仅用于迁移与降级。"""

from dataclasses import dataclass
from enum import StrEnum

from jobpilot.db.memory_schema import BulletLength, TechnicalTerms, WritingTone


class MemoryKey(StrEnum):
    TARGET_DIRECTION = "target_direction"
    CITIES = "cities"
    EXCLUDED_TYPES = "excluded_types"
    JOB_NOTES = "job_notes"
    TONE = "tone"
    BULLET_LENGTH = "bullet_length"
    TECH_TERMS = "tech_terms"


WRITING_CHOICE_TYPES = {
    MemoryKey.TONE: WritingTone,
    MemoryKey.BULLET_LENGTH: BulletLength,
    MemoryKey.TECH_TERMS: TechnicalTerms,
}


@dataclass(frozen=True)
class MemoryLimit:
    characters: int = 0
    items: int = 0
    choices: tuple[str, ...] = ()


MEMORY_LIMITS = {
    MemoryKey.TARGET_DIRECTION: MemoryLimit(characters=100),
    MemoryKey.CITIES: MemoryLimit(characters=20, items=5),
    MemoryKey.EXCLUDED_TYPES: MemoryLimit(characters=30, items=5),
    MemoryKey.JOB_NOTES: MemoryLimit(characters=300),
    **{
        key: MemoryLimit(choices=tuple(choice.value for choice in choices))
        for key, choices in WRITING_CHOICE_TYPES.items()
    },
}
MEMORY_KEY_LENGTH = max(map(len, MEMORY_LIMITS))
MEMORY_REVISION_CHECK = "revision > 0"
MEMORY_SOURCE_CHECK = """
    (source = '{"kind":"manual"}'::jsonb OR
        (source->>'kind' = 'conversation'
         AND jsonb_typeof(source->'conversation_id') = 'string'
         AND jsonb_typeof(source->'message_id') = 'string')) IS TRUE
"""


def _value_constraint(limit: MemoryLimit) -> str:
    if limit.choices:
        choices = ", ".join(f"'{choice}'" for choice in limit.choices)
        return (
            "jsonb_typeof(value_json) = 'string' AND "
            f"value_json #>> '{{}}' IN ({choices})"
        )
    if limit.items:
        # CASE 先确认类型，数组长度函数只接受 JSON 数组。
        return (
            "CASE WHEN jsonb_typeof(value_json) = 'array' THEN "
            f"jsonb_array_length(value_json) <= {limit.items} AND "
            "NOT jsonb_path_exists(value_json, "
            '\'strict $[*] ? (@.type() != "string" || '
            f'!(@ like_regex "^.{{1,{limit.characters}}}$" flag "s"))\') '
            "ELSE false END"
        )
    return (
        "jsonb_typeof(value_json) = 'string' AND "
        f"char_length(value_json #>> '{{}}') BETWEEN 1 AND {limit.characters}"
    )


MEMORY_KEY_CHECK = "key IN (" + ", ".join(f"'{key}'" for key in MEMORY_LIMITS) + ")"
MEMORY_VALUE_CHECK = (
    "(deleted_at IS NOT NULL AND value_json IS NULL) OR "
    "(deleted_at IS NULL AND value_json IS NOT NULL AND CASE key "
    + " ".join(
        f"WHEN '{key}' THEN ({_value_constraint(limit)})"
        for key, limit in MEMORY_LIMITS.items()
    )
    + " ELSE false END)"
)
