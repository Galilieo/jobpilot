"""把已确认的写作选项转成限定措辞，供改写运行以快照保存。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.memories import list_memories
from jobpilot.db.memory_schema import (
    WRITING_CHOICE_TYPES,
    BulletLength,
    TechnicalTerms,
    WritingTone,
)


class WritingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tone: WritingTone | None = None
    bullet_length: BulletLength | None = None
    tech_terms: TechnicalTerms | None = None

    def append_to(self, system: str) -> str:
        instructions = [
            choice.instruction
            for choice in (self.tone, self.bullet_length, self.tech_terms)
            if choice is not None
        ]
        if not instructions:
            return system
        return system + "\n\n写作偏好\n" + "\n".join(instructions)


async def confirmed_writing_preferences(
    session: AsyncSession, user_id: UUID
) -> WritingPreferences:
    return WritingPreferences.model_validate(
        {
            row.key: row.value_json
            for row in await list_memories(user_id, session)
            if row.key in WRITING_CHOICE_TYPES and row.deleted_at is None
        }
    )


async def writing_preferences_snapshot(
    session: AsyncSession, user_id: UUID
) -> dict[str, str] | None:
    preferences = await confirmed_writing_preferences(session, user_id)
    return preferences.model_dump(mode="json", exclude_none=True) or None
