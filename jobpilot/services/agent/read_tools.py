"""Agent 读取工具从登录身份加载岗位、发布事实、报告和简历。"""

from dataclasses import asdict
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.applications import latest_job_resume
from jobpilot.db.conversations import (
    find_conversation,
    latest_agent_run,
    latest_confirmation,
)
from jobpilot.db.jobs import get_user_job, list_user_jobs
from jobpilot.db.models import (
    AiRun,
    Conversation,
    FactSet,
    JobPosting,
    MatchReport,
    UserJob,
)
from jobpilot.services import drafts
from jobpilot.services.agent.arguments import (
    FactsArgs,
    JobArgs,
    MatchReportArgs,
    ResumeArgs,
)
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.jobs import ParsedJob, effective_parsed_job
from jobpilot.services.llm_gateway import preview_text
from jobpilot.services.matching import load_match_inputs
from jobpilot.services.resume_versions import ResumeNotFoundError, load_resume

_MAX_ROWS = 30


async def job_for_tool(
    context: ToolContext, session: AsyncSession, job_id: UUID | None
) -> tuple[JobPosting, UserJob]:
    user_id = context.agent.user_id
    if job_id is None:
        conversation = await find_conversation(
            user_id, session, context.agent.conversation_id
        )
        job_id = conversation.current_job_id if conversation else None
    if job_id is None:
        raise ToolError("当前未绑定岗位，请指定岗位")
    row = await get_user_job(session, user_id, job_id)
    if row is None:
        raise ToolError("岗位不存在", 404)
    return row


async def list_jobs(context: ToolContext, args: ToolArgs) -> Payload:
    async with context.agent.sessions() as session:
        jobs = await list_user_jobs(session, context.agent.user_id)
    return {
        "jobs": [
            {
                "job_id": str(job.user_job_id),
                "fields": effective_parsed_job(posting, job).basic_fields.model_dump(
                    mode="json"
                ),
            }
            for posting, job in jobs[:_MAX_ROWS]
        ],
        "total": len(jobs),
    }


async def read_job(context: ToolContext, args: JobArgs) -> Payload:
    async with context.agent.sessions() as session:
        posting, job = await job_for_tool(context, session, args.job_id)
    return {
        "job_id": str(job.user_job_id),
        "parsed": effective_parsed_job(posting, job).model_dump(mode="json"),
        "raw_text": posting.raw_text,
    }


async def read_facts(context: ToolContext, args: FactsArgs) -> Payload:
    user_id = context.agent.user_id
    async with context.agent.sessions() as session:
        if args.fact_set_id is None:
            fact_set = await drafts.latest_fact_set(session, user_id)
        else:
            fact_set = await session.scalar(
                select(FactSet).where(
                    FactSet.user_id == user_id, FactSet.fact_set_id == args.fact_set_id
                )
            )
        if fact_set is None:
            raise ToolError("已发布事实库不存在", 404)
        inputs = await load_match_inputs(session, user_id, fact_set.fact_set_id)
    return {
        "fact_set_id": str(fact_set.fact_set_id),
        "version": fact_set.version,
        "facts": [fact.model_dump(mode="json") for fact in inputs.facts],
        "criteria": [asdict(item) for item in inputs.criteria],
        "education": [asdict(item) for item in inputs.education],
    }


async def read_match_report(context: ToolContext, args: MatchReportArgs) -> Payload:
    async with context.agent.sessions() as session:
        report = await session.scalar(
            select(MatchReport).where(
                MatchReport.user_id == context.agent.user_id,
                MatchReport.report_id == args.report_id,
            )
        )
        if report is None:
            raise ToolError("匹配报告不存在", 404)
        run = await session.get_one(AiRun, report.run_id)
        location = None
        if run.fact_set_id is not None and run.job_snapshot_json is not None:
            inputs = await load_match_inputs(
                session, context.agent.user_id, run.fact_set_id
            )
            snapshot = ParsedJob.model_validate(run.job_snapshot_json)
            location = inputs.location_check(snapshot.basic_fields.location)
    return {
        "report_id": str(report.report_id),
        "items": report.items_json,
        "main_gaps": report.main_gaps,
        "location_check": asdict(location) if location else None,
        "job_snapshot": run.job_snapshot_json,
        "fact_set_id": str(run.fact_set_id),
    }


async def read_resume(context: ToolContext, args: ResumeArgs) -> Payload:
    async with context.agent.sessions() as session:
        try:
            loaded = await load_resume(
                session,
                context.agent.user_id,
                args.run_id,
                args.version,
                include_history=False,
            )
        except ResumeNotFoundError:
            raise ToolError("定制简历不存在", 404) from None
    return {
        "run_id": str(args.run_id),
        "version_id": str(loaded.version.resume_version_id),
        "version": loaded.version.version,
        "content": loaded.content.model_dump(mode="json"),
    }


def _job_version(snapshot: dict[str, Any]) -> str:
    # 岗位个人覆盖没有独立修订列，用确定性快照标识区分报告基于哪个版本。
    serialized = dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(serialized.encode()).hexdigest()[:16]


async def _latest_report(
    session: AsyncSession, user_id: UUID, job_id: UUID
) -> tuple[MatchReport, AiRun] | None:
    result = await session.execute(
        select(MatchReport, AiRun)
        .join(AiRun, AiRun.run_id == MatchReport.run_id)
        .where(MatchReport.user_id == user_id, AiRun.user_job_id == job_id)
        .order_by(MatchReport.seq.desc())
        .limit(1)
    )
    row = result.first()
    return (row[0], row[1]) if row else None


async def _latest_resume(
    session: AsyncSession, user_id: UUID, job_id: UUID
) -> Payload | None:
    latest = await latest_job_resume(user_id, session, job_id, include_content=False)
    if latest is None:
        return None
    version, _, run = latest
    return {"run_id": str(run.run_id), "version": version.version}


async def current_job_summary(
    user_id: UUID,
    session: AsyncSession,
    conversation: Conversation,
) -> Payload:
    """只放对象标识和版本，详情由读取工具按需取得。"""
    job_id = conversation.current_job_id
    fact = await drafts.latest_fact_set(session, user_id)
    latest = await latest_agent_run(
        user_id, session, conversation.conversation_id, active_only=True
    )
    action = (
        await latest_confirmation(user_id, session, latest.run_id) if latest else None
    )
    state = {
        "binding_revision": conversation.job_binding_revision,
        "facts": {"id": str(fact.fact_set_id), "version": fact.version}
        if fact
        else None,
        "active_card": {"id": str(action.action_id), "status": action.status}
        if action
        else None,
    }
    row = await get_user_job(session, user_id, job_id) if job_id else None
    if row is None or job_id is None:
        return {**state, "job": None, "match": "未匹配", "resume": None}
    parsed = effective_parsed_job(*row)
    snapshot = parsed.model_dump(mode="json")
    latest_report = await _latest_report(session, user_id, job_id)
    match: Payload | str = "未匹配"
    if latest_report is not None:
        report, run = latest_report
        based_on = run.job_snapshot_json or {}
        stale = (
            based_on != snapshot or fact is None or fact.fact_set_id != run.fact_set_id
        )
        match = {
            "report_id": str(report.report_id),
            "job_version": _job_version(based_on),
            "fact_set_id": str(run.fact_set_id),
            "status": "基于旧版本" if stale else "当前版本",
        }
    return {
        **state,
        "job": {
            "id": str(job_id),
            "version": _job_version(snapshot),
            "title": preview_text(parsed.basic_fields.title or "", 80),
            "company": preview_text(parsed.basic_fields.company or "", 80),
        },
        "match": match,
        "resume": await _latest_resume(session, user_id, job_id),
    }
