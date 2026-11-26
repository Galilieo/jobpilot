"""把已确认的写作选项转成限定措辞，供改写运行以快照保存。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.memories import list_memories
from jobpilot.db.memory_schema import MemoryKey

_INSTRUCTIONS = {
    MemoryKey.TONE: {
        "平实": "语气平实，使用具体描述。",
        "简洁有力": "语气简洁有力，使用准确的动词。",
    },
    MemoryKey.BULLET_LENGTH: {
        "尽量一行": "每条尽量写成一行的长度。",
        "不超过两行": "控制条目长度，尽量控制在两行内。",
        "不限": "条目可以根据事实需要使用合适的长度。",
    },
    MemoryKey.TECH_TERMS: {
        "英文原名": "技术名称使用英文原名。",
        "中文": "有通用中文译名的技术名称使用中文。",
        "中英并列": "有通用中文译名的技术名称使用中英并列。",
    },
}


class WritingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tone: Literal["平实", "简洁有力"] | None = None
    bullet_length: Literal["尽量一行", "不超过两行", "不限"] | None = None
    tech_terms: Literal["英文原名", "中文", "中英并列"] | None = None

    def append_to(self, system: str) -> str:
        values = self.model_dump(exclude_none=True)
        if not values:
            return system
        instructions = [
            _INSTRUCTIONS[MemoryKey(key)][value] for key, value in values.items()
        ]
        return system + "\n\n写作偏好\n" + "\n".join(instructions)


async def confirmed_writing_preferences(
    session: AsyncSession, user_id: UUID
) -> WritingPreferences:
    return WritingPreferences.model_validate(
        {
            row.key: row.value_json
            for row in await list_memories(user_id, session)
            if row.key in _INSTRUCTIONS and row.deleted_at is None
        }
    )


async def writing_preferences_snapshot(
    session: AsyncSession, user_id: UUID
) -> dict[str, str] | None:
    preferences = await confirmed_writing_preferences(session, user_id)
    return preferences.model_dump(exclude_none=True) or None
