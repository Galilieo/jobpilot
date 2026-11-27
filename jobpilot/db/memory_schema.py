"""记忆栏目与持久化限额，供数据库约束和应用校验共用。"""

from dataclasses import dataclass
from enum import StrEnum


class MemoryKey(StrEnum):
    TARGET_DIRECTION = "target_direction"
    CITIES = "cities"
    EXCLUDED_TYPES = "excluded_types"
    JOB_NOTES = "job_notes"
    TONE = "tone"
    BULLET_LENGTH = "bullet_length"
    TECH_TERMS = "tech_terms"


class WritingChoice(StrEnum):
    instruction: str

    def __new__(cls, value: str, instruction: str):
        if not instruction.strip():
            raise ValueError(f"写作选项「{value}」缺少固定措辞")
        choice = str.__new__(cls, value)
        choice._value_ = value
        choice.instruction = instruction
        return choice


class WritingTone(WritingChoice):
    PLAIN = "平实", "语气平实，使用具体描述。"
    CONCISE = "简洁有力", "语气简洁有力，使用准确的动词。"


class BulletLength(WritingChoice):
    ONE_LINE = "尽量一行", "每条尽量写成一行的长度。"
    TWO_LINES = "不超过两行", "控制条目长度，尽量控制在两行内。"
    UNRESTRICTED = "不限", "条目可以根据事实需要使用合适的长度。"


class TechnicalTerms(WritingChoice):
    ENGLISH = "英文原名", "技术名称使用英文原名。"
    CHINESE = "中文", "有通用中文译名的技术名称使用中文。"
    BILINGUAL = "中英并列", "有通用中文译名的技术名称使用中英并列。"


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
