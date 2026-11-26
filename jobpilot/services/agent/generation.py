"""用会话剩余额度调用已有的 JD、匹配与改写服务，保存独立业务运行。"""

import asyncio
from hashlib import sha256
from uuid import UUID, uuid5

from openai import APIError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import agent_input
from jobpilot.db.jobs import find_existing_job
from jobpilot.db.models import (
    AiRun,
    JobPosting,
    MatchReport,
    ResumeDocument,
    RunStatus,
    UserJob,
)
from jobpilot.db.reports import add_match_report
from jobpilot.db.runs import finish_run, set_run_status
from jobpilot.services import drafts
from jobpilot.services.agent.arguments import JobArgs, ParseJobArgs, TailorArgs
from jobpilot.services.agent.read_tools import job_for_tool
from jobpilot.services.agent.types import Payload, ToolContext, ToolError
from jobpilot.services.injection import detect_injection
from jobpilot.services.jobs import ParsedJob, effective_parsed_job, parse_job
from jobpilot.services.llm_gateway import BudgetExhaustedError
from jobpilot.services.matching import (
    MatchValidationError,
    load_match_inputs,
    match_job,
)
from jobpilot.services.parsing import normalize_text
from jobpilot.services.tailoring import MODEL_ERRORS
from jobpilot.services.tailoring_graph import (
    TAILOR_MAX_CONCURRENT_REQUESTS,
    TailorDeps,
    run_tailoring,
)
from jobpilot.services.tailoring_plan import (
    TailorFact,
    UnresolvedSectionsError,
    resolve_sections,
)
from jobpilot.services.tailoring_store import TailorInputError


def _child_id(context: ToolContext) -> UUID:
    return uuid5(context.agent.run_id, context.call["key"])


async def _finish(context: ToolContext, run_id: UUID, status: RunStatus) -> None:
    async with context.agent.sessions.begin() as session:
        await set_run_status(session, context.agent.user_id, run_id, status)


async def _save_job(
    context: ToolContext, raw_text: str, content_hash: str, parsed: ParsedJob
) -> UUID:
    user_id, run_id = context.agent.user_id, _child_id(context)
    async with context.agent.sessions.begin() as session:
        posting_id = await session.scalar(
            insert(JobPosting)
            .values(
                owner_id=user_id,
                raw_text=raw_text,
                content_hash=content_hash,
                parsed_json=parsed.model_dump(mode="json"),
                keywords=parsed.keywords,
                injection_flags=[flag.to_json() for flag in detect_injection(raw_text)],
            )
            .on_conflict_do_nothing(
                index_elements=[JobPosting.owner_id, JobPosting.content_hash]
            )
            .returning(JobPosting.posting_id)
        )
        if posting_id is None:
            job_id = await find_existing_job(session, user_id, content_hash)
            if job_id is None:
                raise ToolError("岗位已变更，请重试", 409)
        else:
            job = UserJob(user_id=user_id, posting_id=posting_id)
            session.add(job)
            await session.flush()
            job_id = job.user_job_id
        await finish_run(session, user_id, run_id, RunStatus.SUCCEEDED)
        run = await session.get_one(AiRun, run_id)
        run.user_job_id = job_id
    return job_id


async def _jd_source_text(context: ToolContext, args: ParseJobArgs) -> str:
    """引用只能指向本轮输入，返回服务端保存的原文。"""
    agent = context.agent
    async with agent.sessions() as session:
        message = await agent_input(agent.user_id, session, agent.run_id)
    if message is None:
        raise ToolError("本轮消息不存在")
    if args.source == "message":
        return message.content
    if args.source > len(message.attachments_json):
        raise ToolError("附件序号不属于本轮消息")
    return message.attachments_json[args.source - 1]["text"]


async def parse_jd(context: ToolContext, args: ParseJobArgs) -> Payload:
    user_id, run_id = context.agent.user_id, _child_id(context)
    raw_text = normalize_text(await _jd_source_text(context, args)).strip()
    if not raw_text:
        raise ToolError("请提供 JD 正文")
    content_hash = sha256(raw_text.encode("utf-8")).hexdigest()
    async with context.agent.sessions.begin() as session:
        existing = await find_existing_job(session, user_id, content_hash)
        if existing is not None:
            return {"job_id": str(existing), "status": "succeeded"}
        if await session.get(AiRun, run_id) is not None:
            raise ToolError("这次解析已中断，请重新发起")
        session.add(AiRun(run_id=run_id, user_id=user_id, kind="parse_jd"))
    try:
        parsed = await parse_job(
            context.agent.sessions,
            user_id,
            run_id,
            client=context.agent.client,
            budget=context.budget,
            raw_text=raw_text,
        )
        job_id = await _save_job(context, raw_text, content_hash, parsed)
    except (*MODEL_ERRORS, asyncio.CancelledError, BudgetExhaustedError) as error:
        await _record_generation_failure(context, run_id, error)
        raise
    return {"job_id": str(job_id), "status": "succeeded"}


async def _record_generation_failure(
    context: ToolContext, run_id: UUID, error: BaseException
) -> None:
    status = (
        RunStatus.BUDGET_EXHAUSTED
        if isinstance(error, (BudgetExhaustedError, asyncio.CancelledError))
        else RunStatus.FAILED
    )
    await _finish(context, run_id, status)


async def _match_report_id(
    session: AsyncSession, user_id: UUID, run_id: UUID
) -> UUID | None:
    return await session.scalar(
        select(MatchReport.report_id).where(
            MatchReport.run_id == run_id, MatchReport.user_id == user_id
        )
    )


async def match(context: ToolContext, args: JobArgs) -> Payload:
    user_id, run_id = context.agent.user_id, _child_id(context)
    async with context.agent.sessions.begin() as session:
        existing = await _match_report_id(session, user_id, run_id)
        if existing is not None:
            return {"report_id": str(existing), "run_id": str(run_id)}
        if await session.get(AiRun, run_id) is not None:
            raise ToolError("这次匹配已中断，请重新发起")
        posting, job = await job_for_tool(context, session, args.job_id)
        snapshot = effective_parsed_job(posting, job)
        fact_set = await drafts.latest_fact_set(session, user_id)
        if fact_set is None or not snapshot.requirements:
            raise ToolError("请先发布事实库，并补充岗位要求")
        inputs = await load_match_inputs(session, user_id, fact_set.fact_set_id)
        if inputs.is_empty:
            raise ToolError("事实库暂时没有可匹配的内容")
        run = AiRun(
            run_id=run_id,
            user_id=user_id,
            kind="match",
            user_job_id=job.user_job_id,
            fact_set_id=fact_set.fact_set_id,
            job_snapshot_json=snapshot.model_dump(mode="json"),
        )
        session.add(run)
    try:
        result = await match_job(
            context.agent.sessions,
            user_id,
            run_id,
            fact_set_id=fact_set.fact_set_id,
            client=context.agent.client,
            budget=context.budget,
            snapshot=snapshot,
            facts=inputs.facts,
            criteria=inputs.criteria,
            education=inputs.education,
        )
    except (
        *MODEL_ERRORS,
        MatchValidationError,
        asyncio.CancelledError,
        BudgetExhaustedError,
    ) as error:
        await _record_generation_failure(context, run_id, error)
        raise
    async with context.agent.sessions.begin() as session:
        report = add_match_report(
            session,
            run,
            items=[item.model_dump(mode="json") for item in result.items],
            main_gaps=result.main_gaps,
        )
        await finish_run(session, user_id, run_id, RunStatus.SUCCEEDED)
        await session.flush()
    return {"report_id": str(report.report_id), "run_id": str(run_id)}


async def _prepare_tailor(context: ToolContext, args: TailorArgs) -> AiRun:
    user_id, run_id = context.agent.user_id, _child_id(context)
    async with context.agent.sessions.begin() as session:
        existing = await session.scalar(
            select(AiRun).where(
                AiRun.run_id == run_id,
                AiRun.user_id == user_id,
            )
        )
        if existing is not None:
            return existing
        posting, job = await job_for_tool(context, session, args.job_id)
        snapshot = effective_parsed_job(posting, job)
        base_doc_id = await _base_document(session, user_id, args.base_doc_id)
        fact_set = await drafts.latest_fact_set(session, user_id)
        facts = await drafts.facts_of(session, fact_set)
        if fact_set is None or not facts or not snapshot.requirements:
            raise ToolError("请先发布经历事实，并补充岗位要求")
        try:
            resolve_sections([TailorFact.of(fact) for fact in facts], args.sections)
        except UnresolvedSectionsError as error:
            raise ToolError(f"请指定经历栏目：{'、'.join(error.experiences)}") from None
        run = AiRun(
            run_id=run_id,
            user_id=user_id,
            kind="tailor",
            user_job_id=job.user_job_id,
            base_doc_id=base_doc_id,
            fact_set_id=fact_set.fact_set_id,
            job_snapshot_json=snapshot.model_dump(mode="json"),
        )
        session.add(run)
    return run


async def _base_document(
    session: AsyncSession, user_id: UUID, doc_id: UUID | None
) -> UUID:
    query = select(ResumeDocument.doc_id).where(ResumeDocument.user_id == user_id)
    if doc_id:
        found = await session.scalar(query.where(ResumeDocument.doc_id == doc_id))
    else:
        found = await session.scalar(
            query.order_by(ResumeDocument.created_at.desc()).limit(1)
        )
    if found is None:
        raise ToolError("简历底稿不存在", 404 if doc_id else 422)
    return found


async def tailor(context: ToolContext, args: TailorArgs) -> Payload:
    run = await _prepare_tailor(context, args)
    if run.status == RunStatus.RUNNING:
        deps = TailorDeps(
            context.agent.sessions,
            context.agent.client,
            context.budget,
            asyncio.Semaphore(TAILOR_MAX_CONCURRENT_REQUESTS),
        )
        try:
            await run_tailoring(
                deps, context.agent.user_id, run.run_id, section_overrides=args.sections
            )
        except (asyncio.CancelledError, TailorInputError, APIError) as error:
            await _record_generation_failure(context, run.run_id, error)
            raise
    async with context.agent.sessions() as session:
        finished = await session.get_one(AiRun, run.run_id)
    return {
        "run_id": str(run.run_id),
        "status": finished.status,
        "base_doc_id": str(run.base_doc_id),
        "stop_reason": finished.stop_reason,
    }
