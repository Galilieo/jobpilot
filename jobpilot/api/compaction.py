"""接收手动压缩请求，按用户隔离启动或查询压缩运行。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from jobpilot.api.deps import CurrentUser, ModelClient, SessionFactory
from jobpilot.api.tailoring import Launcher
from jobpilot.services.agent.compaction import (
    CompactConflict,
    CompactResult,
    CompactSchedule,
    schedule_compaction,
)

router = APIRouter(prefix="/api/conversations", tags=["agent"])


class CompactIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    focus: str | None = Field(default=None, min_length=1, max_length=300)


@router.post("/{conversation_id}/compact")
async def compact(
    conversation_id: UUID,
    response: Response,
    user: CurrentUser,
    sessions: SessionFactory,
    model: ModelClient,
    launcher: Launcher,
    body: CompactIn | None = None,
) -> CompactResult:
    try:
        result = await schedule_compaction(
            CompactSchedule(
                sessions,
                model,
                launcher.launch,
                user.user_id,
                conversation_id,
                focus=body.focus if body else None,
            )
        )
    except CompactConflict as error:
        raise HTTPException(error.status, str(error)) from None
    if result.run_id is not None:
        response.status_code = 202
    return result
