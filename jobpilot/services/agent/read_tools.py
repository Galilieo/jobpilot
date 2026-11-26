"""Agent 读取工具从登录身份加载岗位、发布事实、报告和简历。"""

from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import find_conversation
from jobpilot.db.jobs import get_user_job, list_user_jobs
from jobpilot.db.models import AiRun, FactSet, JobPosting, MatchReport, UserJob
from jobpilot.db.reports import latest_match_reports_by_job
from jobpilot.services import drafts
from jobpilot.services.agent.arguments import (
    FactsArgs,
    JobArgs,
    MatchReportArgs,
    ResumeArgs,
)
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.jobs import effective_parsed_job
from jobpilot.services.matching import load_match_inputs, summarize_stored_match
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
    return {
        "report_id": str(report.report_id),
        "items": report.items_json,
        "main_gaps": report.main_gaps,
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


async def current_job_summary(
    user_id: UUID, session: AsyncSession, job_id: UUID | None
) -> dict[str, Any] | None:
    row = await get_user_job(session, user_id, job_id) if job_id else None
    if row is None:
        return None
    parsed = effective_parsed_job(*row)
    reports = await latest_match_reports_by_job(session, user_id)
    snapshot, items = reports.get(job_id, (None, []))
    summary = summarize_stored_match(snapshot, items) if snapshot else None
    return {
        "job_id": str(job_id),
        "title": parsed.basic_fields.title,
        "hard_requirements": [
            requirement.text
            for requirement in parsed.requirements
            if requirement.type == "hard"
        ],
        "match": summary.model_dump(mode="json") if summary is not None else None,
    }
