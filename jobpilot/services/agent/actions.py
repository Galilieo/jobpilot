"""写入卡在执行前固定参数，事务内共同提交写入、结果和撤销信息。"""

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import agent_input, find_conversation, set_job_binding
from jobpilot.db.jobs import JobLockConflict, get_user_job, lock_job_for_binding
from jobpilot.db.models import ActionStatus, AgentAction, AiRun, Conversation
from jobpilot.services import drafts
from jobpilot.services.agent.application_actions import undo_application
from jobpilot.services.agent.arguments import EditResumeArgs, SetJobArgs, ShortText
from jobpilot.services.agent.edit_request import authorized_edit
from jobpilot.services.agent.memory_actions import undo_memory
from jobpilot.services.agent.types import Payload, ToolContext, ToolError
from jobpilot.services.grounding import GroundingFact, organizations_of
from jobpilot.services.jobs import effective_parsed_job
from jobpilot.services.resume_content import (
    Bullet,
    BulletCheck,
    CheckLayers,
    ResumeContent,
    plain,
)
from jobpilot.services.resume_versions import (
    ResumeNotFoundError,
    VersionConflictError,
    load_resume,
    recheck,
    restore_snapshot,
    save_checked_version,
)
from jobpilot.services.rewriting import edit_bullet
from jobpilot.services.semantic_check import ground_check
from jobpilot.services.writing_preferences import confirmed_writing_preferences


class PreparedResume(BaseModel):
    run_id: UUID
    bullet_id: UUID
    text: ShortText
    base_version_id: UUID
    previous_text: str
    check: BulletCheck


class PreparedJob(SetJobArgs):
    title: str | None = None


class JobChange(BaseModel):
    job_id: UUID | None
    previous_job_id: UUID | None
    applied_revision: int | None = None


def bind_job(conversation: Conversation, job_id: UUID | None) -> JobChange:
    """更新已锁住的对话绑定，记下撤销所需的旧岗位和动作生效的修订号。"""
    previous = conversation.current_job_id
    revision = set_job_binding(conversation, job_id)
    return JobChange(job_id=job_id, previous_job_id=previous, applied_revision=revision)


class ResumeUndo(BaseModel):
    run_id: UUID
    previous_version_id: UUID
    applied_version_id: UUID


@dataclass(frozen=True)
class AppliedAction:
    result: Payload
    undo: Payload


def _bullet(content: ResumeContent, bullet_id: UUID) -> Bullet:
    for section in content.sections:
        for entry in section.entries:
            for bullet in entry.bullets:
                if bullet.bullet_id == bullet_id:
                    return bullet
    raise ToolError("简历条目不存在", 404)


async def _resume_facts(
    session: AsyncSession, user_id: UUID, run_id: UUID
) -> dict[UUID, GroundingFact]:
    run = await session.scalar(
        select(AiRun).where(
            AiRun.user_id == user_id, AiRun.run_id == run_id, AiRun.kind == "tailor"
        )
    )
    if run is None or run.fact_set_id is None:
        raise ToolError("定制简历不存在", 404)
    return {
        fact.fact_id: GroundingFact.of(fact)
        for fact in await drafts.facts_in(session, user_id, run.fact_set_id)
    }


async def _checked_edit(
    context: ToolContext,
    draft: ResumeContent,
    bullet_id: UUID,
    text: str,
    facts: dict[UUID, GroundingFact],
) -> BulletCheck:
    before = draft.model_copy(deep=True)
    _bullet(draft, bullet_id).text = text
    checked = _bullet(
        recheck(before, draft, facts, organizations_of(facts.values())), bullet_id
    )
    agent = context.agent
    semantic = await ground_check(
        agent.sessions,
        agent.user_id,
        agent.run_id,
        client=agent.client,
        budget=context.budget,
        sentence=plain(checked.text),
        facts=[facts[fact_id] for fact_id in checked.fact_ids if fact_id in facts],
    )
    return BulletCheck(
        text=checked.text,
        layers=CheckLayers.FULL,
        issues=list(dict.fromkeys((*checked.check.issues, *semantic.issues))),
    )


async def prepare_resume(context: ToolContext, args: EditResumeArgs) -> Payload:
    agent = context.agent
    async with agent.sessions() as session:
        loaded = await load_resume(
            session, agent.user_id, args.run_id, include_history=False
        )
        draft = loaded.content
        bullet = _bullet(draft, args.bullet_id)
        previous_text = bullet.text
        message = await agent_input(agent.user_id, session, agent.run_id)
        if context.message_used_as_jd or not authorized_edit(message, draft, args):
            return {
                "error": "本轮修改的条目或文字不明确",
                "status": 422,
                "clarify": "请指出要改的简历条目，并给出完整替换正文或润色要求。",
            }
        facts = await _resume_facts(session, agent.user_id, args.run_id)
        preferences = await confirmed_writing_preferences(session, agent.user_id)
    text = args.quote
    if args.mode == "polish":
        cited = [facts[fact_id] for fact_id in bullet.fact_ids if fact_id in facts]
        if not cited:
            raise ToolError("这条没有可用的事实引用，请给出完整替换正文")
        text = await edit_bullet(
            agent.sessions,
            agent.user_id,
            agent.run_id,
            client=agent.client,
            budget=context.budget,
            previous_text=previous_text,
            facts=cited,
            quote=args.quote,
            writing_preferences=preferences,
        )
    check = await _checked_edit(context, draft, args.bullet_id, text, facts)
    return PreparedResume(
        run_id=args.run_id,
        bullet_id=args.bullet_id,
        text=check.text,
        base_version_id=loaded.version.resume_version_id,
        previous_text=previous_text,
        check=check,
    ).model_dump(mode="json")


async def prepare_job(context: ToolContext, args: SetJobArgs) -> Payload:
    if args.job_id is None:
        return PreparedJob(job_id=None).model_dump(mode="json")
    async with context.agent.sessions() as session:
        row = await get_user_job(session, context.agent.user_id, args.job_id)
    if row is None:
        raise ToolError("岗位不存在", 404)
    title = effective_parsed_job(*row).basic_fields.title
    return PreparedJob(job_id=args.job_id, title=title).model_dump(mode="json")


async def _apply_job(session: AsyncSession, action: AgentAction) -> AppliedAction:
    args = PreparedJob.model_validate(action.args_json)
    conversation = await find_conversation(
        action.user_id, session, action.conversation_id, lock=True
    )
    if conversation is None:
        raise ToolError("对话不存在", 404)
    if args.job_id is not None and not await lock_agent_job(
        session, action.user_id, args.job_id
    ):
        raise ToolError("岗位不存在", 404)
    change = bind_job(conversation, args.job_id)
    return AppliedAction(
        {"job_id": str(args.job_id) if args.job_id else None},
        change.model_dump(mode="json"),
    )


async def lock_agent_job(session: AsyncSession, user_id: UUID, job_id: UUID) -> bool:
    try:
        return await lock_job_for_binding(session, user_id, job_id)
    except JobLockConflict as error:
        raise ToolError(str(error), 409) from None


async def _apply_resume(session: AsyncSession, action: AgentAction) -> AppliedAction:
    args = PreparedResume.model_validate(action.args_json)
    loaded = await load_resume(
        session, action.user_id, args.run_id, include_history=False
    )
    if loaded.version.resume_version_id != args.base_version_id:
        raise VersionConflictError(loaded.version.resume_version_id)
    content = loaded.content
    bullet = _bullet(content, args.bullet_id)
    bullet.edited = bullet.edited or plain(args.check.text) != plain(bullet.text)
    bullet.text = args.check.text
    bullet.check = args.check
    saved = await save_checked_version(
        session,
        action.user_id,
        args.run_id,
        base_version_id=args.base_version_id,
        content=content,
    )
    version_id = saved.version.resume_version_id
    undo = ResumeUndo(
        run_id=args.run_id,
        previous_version_id=args.base_version_id,
        applied_version_id=version_id,
    )
    return AppliedAction(
        {
            "run_id": str(args.run_id),
            "version_id": str(version_id),
            "check": args.check.model_dump(mode="json"),
        },
        undo.model_dump(mode="json"),
    )


async def verify_action_version(session: AsyncSession, action: AgentAction) -> None:
    if action.tool == "edit_resume_item":
        args = PreparedResume.model_validate(action.args_json)
        try:
            loaded = await load_resume(
                session, action.user_id, args.run_id, include_history=False
            )
        except ResumeNotFoundError:
            raise ToolError("定制简历不存在", 404) from None
        if loaded.version.resume_version_id != args.base_version_id:
            raise ToolError("简历已在别处保存，请重新发起修改", 409)


async def apply_write(session: AsyncSession, action: AgentAction) -> Payload:
    """锁由调用方持有，重复运行时直接读取已提交结果。"""
    if action.result_json is not None:
        return action.result_json
    apply = {"set_current_job": _apply_job, "edit_resume_item": _apply_resume}[
        action.tool
    ]
    try:
        applied = await apply(session, action)
    except (VersionConflictError, ResumeNotFoundError, ToolError) as error:
        if isinstance(error, VersionConflictError):
            result = {"error": "简历已在别处保存，请重新发起修改", "status": 409}
        elif isinstance(error, ResumeNotFoundError):
            result = {"error": "定制简历不存在", "status": 404}
        else:
            result = {"error": str(error), "status": error.status}
        action.status = ActionStatus.FAILED
    else:
        result = applied.result
        action.undo_json = applied.undo
        action.status = ActionStatus.EXECUTED
    action.result_json = result
    return result


async def _undo_job(
    session: AsyncSession, action: AgentAction, undo: Payload
) -> Payload:
    change = JobChange.model_validate(undo)
    conversation = await find_conversation(
        action.user_id, session, action.conversation_id, lock=True
    )
    if (
        conversation is None
        or conversation.job_binding_revision != change.applied_revision
    ):
        raise ToolError("当前岗位已变更，无法撤销", 409)
    if change.previous_job_id and not await lock_agent_job(
        session, action.user_id, change.previous_job_id
    ):
        raise ToolError("原岗位已删除，无法撤销", 409)
    set_job_binding(conversation, change.previous_job_id)
    return {"job_id": str(change.previous_job_id) if change.previous_job_id else None}


async def _undo_resume(
    session: AsyncSession, action: AgentAction, undo: Payload
) -> Payload:
    change = ResumeUndo.model_validate(undo)
    try:
        loaded = await restore_snapshot(
            session,
            action.user_id,
            change.run_id,
            base_version_id=change.applied_version_id,
            restore_version_id=change.previous_version_id,
        )
    except (VersionConflictError, ResumeNotFoundError):
        raise ToolError("简历版本已变更，无法撤销", 409) from None
    return {"version_id": str(loaded.version.resume_version_id)}


async def undo_write(session: AsyncSession, action: AgentAction) -> Payload:
    if action.status == ActionStatus.UNDONE and action.undo_json is not None:
        return action.undo_json["result"]
    if action.status != ActionStatus.EXECUTED or action.undo_json is None:
        raise ToolError("这张卡没有可撤销的写入", 409)
    undo = {
        "set_current_job": _undo_job,
        "ask_user": _undo_job,
        "edit_resume_item": _undo_resume,
        "remember": undo_memory,
        "record_application": undo_application,
        "forget": undo_memory,
    }[action.tool]
    result = await undo(session, action, action.undo_json)
    action.undo_json = {**action.undo_json, "result": result}
    action.status = ActionStatus.UNDONE
    return result
