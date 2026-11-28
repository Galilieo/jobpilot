"""长期记忆与结构化写作偏好的数据库和应用约束。"""

from enum import StrEnum


class MemoryCategory(StrEnum):
    JOB_PREF = "job_pref"
    AGENT_RULE = "agent_rule"


class WritingField(StrEnum):
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
    WritingField.TONE: WritingTone,
    WritingField.BULLET_LENGTH: BulletLength,
    WritingField.TECH_TERMS: TechnicalTerms,
}
WRITING_LABELS = {
    WritingField.TONE: "语气",
    WritingField.BULLET_LENGTH: "每条长度",
    WritingField.TECH_TERMS: "技术名写法",
}

MEMORY_CONTENT_CHARS = 200
MEMORY_BUDGET_CHARS = 1500
# 按日常偏好短句长度设定；正文超长时可能贴入 JD 或转述文字，改为出确认卡。
MEMORY_AUTO_MAX_BODY_CHARS = 300
MEMORY_WRITES_PER_RUN = 3
MEMORY_SHORT_ID_LENGTH = 8
MEMORY_REVISION_CHECK = "revision > 0"
MEMORY_CATEGORY_CHECK = (
    "category IN (" + ", ".join(f"'{item}'" for item in MemoryCategory) + ")"
)
MEMORY_CONTENT_CHECK = f"char_length(content) BETWEEN 1 AND {MEMORY_CONTENT_CHARS}"
MEMORY_UNIQUE_INDEX = "uq_memory_active_content"
MEMORY_NORMALIZED_CONTENT = (
    "regexp_replace(content, '^[[:space:]]+|[[:space:]]+$', '', 'g')"
)
MEMORY_ACTIVE_WHERE = "deleted_at IS NULL"
MEMORY_SOURCE_CHECK = """
    (source = '{"kind":"manual"}'::jsonb OR
        (source->>'kind' = 'conversation'
         AND jsonb_typeof(source->'conversation_id') = 'string'
         AND jsonb_typeof(source->'message_id') = 'string')) IS TRUE
"""


def writing_choice_check(field: WritingField) -> str:
    choices = ", ".join(f"'{choice.value}'" for choice in WRITING_CHOICE_TYPES[field])
    return f"{field} IN ({choices})"
