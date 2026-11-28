"""把已保存的写作选项转成限定措辞，供改写运行以快照保存。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.memories import find_writing_preference
from jobpilot.db.memory_schema import (
    WRITING_CHOICE_TYPES,
    WRITING_LABELS,
    BulletLength,
    TechnicalTerms,
    WritingField,
    WritingTone,
)
from jobpilot.db.models import WritingPreference
from jobpilot.services.memories import MemoryError


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


async def load_writing_preferences(
    session: AsyncSession, user_id: UUID
) -> WritingPreferences:
    row = await find_writing_preference(session, user_id)
    return options_from_row(row)


def options_from_row(row: WritingPreference | None) -> WritingPreferences:
    return (
        WritingPreferences(**{field: getattr(row, field) for field in WritingField})
        if row
        else WritingPreferences()
    )


class WritingPreferenceUpdate(WritingPreferences):
    expected_revision: int = Field(ge=0)


class WritingPreferenceView(BaseModel):
    values: WritingPreferences
    revision: int
    options: dict[WritingField, list[str]]
    labels: dict[WritingField, str]


def writing_preference_view(row: WritingPreference | None) -> WritingPreferenceView:
    return WritingPreferenceView(
        values=options_from_row(row),
        revision=row.revision if row else 0,
        options={
            field: list(choices) for field, choices in WRITING_CHOICE_TYPES.items()
        },
        labels=WRITING_LABELS,
    )


async def save_writing_preference(
    session: AsyncSession,
    user_id: UUID,
    row: WritingPreference | None,
    values: WritingPreferences,
    expected_revision: int,
    source: dict[str, str],
) -> WritingPreference:
    if (row.revision if row else 0) != expected_revision:
        raise MemoryError("写作偏好已在别处更新，请刷新后重试", 409)
    if row is None:
        row = WritingPreference(
            user_id=user_id, **values.model_dump(mode="json"), source=source
        )
        session.add(row)
    else:
        for field in WritingField:
            setattr(row, field, getattr(values, field))
        row.source = source
        row.revision += 1
        row.updated_at = func.now()
    await session.flush()
    return row


async def writing_preferences_snapshot(
    session: AsyncSession, user_id: UUID
) -> dict[str, str] | None:
    preferences = await load_writing_preferences(session, user_id)
    return preferences.model_dump(mode="json", exclude_none=True) or None
