"""投递确认卡绑定具体简历版本，确认与撤销共用投递服务。"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import find_conversation
from jobpilot.db.models import ActionStatus, AgentAction
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.applications import (
    ApplicationError,
    ApplicationInput,
    Channel,
    application_source,
    delete_application,
    record_application,
)


class RecordApplicationArgs(ToolArgs):
    # 省略字段即可采用默认值，工具定义不重复发布 null 分支。
    job_id: UUID | SkipJsonSchema[None] = None
    resume_version_id: UUID | SkipJsonSchema[None] = None
    applied_at: date | SkipJsonSchema[None] = None
    channel: Channel | SkipJsonSchema[None] = None


class ApplicationCard(ApplicationInput):
    applied_at: date
    title: str | None
    company: str | None
    resume_version: int

    def submission(self) -> ApplicationInput:
        return ApplicationInput(
            job_id=self.job_id,
            resume_version_id=self.resume_version_id,
            applied_at=self.applied_at,
            channel=self.channel,
        )


class ApplicationUndo(BaseModel):
    application_id: UUID
    applied_revision: int


async def prepare_application(
    context: ToolContext, args: RecordApplicationArgs
) -> Payload:
    agent = context.agent
    async with agent.sessions() as session:
        conversation = await find_conversation(
            agent.user_id, session, agent.conversation_id
        )
        job_id = args.job_id or (conversation.current_job_id if conversation else None)
        if job_id is None:
            raise ToolError("请先选定已投递的岗位")
        try:
            source = await application_source(
                agent.user_id, session, job_id, args.resume_version_id
            )
        except ApplicationError as error:
            raise ToolError(str(error), error.status) from None
    return ApplicationCard(
        job_id=job_id,
        resume_version_id=source.version.resume_version_id,
        applied_at=args.applied_at or date.today(),
        channel=args.channel,
        title=source.job.title,
        company=source.job.company,
        resume_version=source.version.version,
    ).model_dump(mode="json")


async def confirm_application(session: AsyncSession, action: AgentAction) -> Payload:
    """决定和投递在同一事务保存，恢复对话时可重放原结果。"""
    card = ApplicationCard.model_validate(action.args_json)
    try:
        row = await record_application(
            action.user_id, session, card.submission(), action_id=action.action_id
        )
    except ApplicationError as error:
        raise ToolError(str(error), error.status) from None
    undo = ApplicationUndo(
        application_id=row.application_id, applied_revision=row.revision
    )
    result = {"application_id": str(row.application_id), "job_id": str(card.job_id)}
    action.status = ActionStatus.EXECUTED
    action.result_json = result
    action.undo_json = undo.model_dump(mode="json")
    return result


async def undo_application(
    session: AsyncSession, action: AgentAction, undo: Payload
) -> Payload:
    previous = ApplicationUndo.model_validate(undo)
    try:
        await delete_application(
            action.user_id, session, previous.application_id, previous.applied_revision
        )
    except ApplicationError:
        raise ToolError("投递记录已变更，请到岗位详情处理", 409) from None
    return {"application_id": str(previous.application_id), "deleted": True}
