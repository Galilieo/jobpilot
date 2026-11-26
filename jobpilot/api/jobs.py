"""岗位库接口：提交 JD 并解析、列表、详情、修改。

job_posting 存原文和解析结果，不随个人修改而变；个人的进度、备注和对解析结果的修改
写在 user_job。路径里的 job_id 指 user_job，不是本人的一律 404。
本周 MVP 不做薪资换算、筛选、统计和自定义字段（见 06 执行进度）。
"""

from collections.abc import AsyncIterator
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError

from jobpilot.api.deps import (
    CurrentUser,
    DbSession,
    ModelClient,
    SessionFactory,
    finish_product,
)
from jobpilot.api.idempotency import RequiredKey, idempotent_request, save_snapshot
from jobpilot.api.tailoring import Launcher
from jobpilot.db.idempotency import Attempt, KeyScope, Operation
from jobpilot.db.jobs import (
    delete_user_job,
    find_existing_job,
    get_user_job,
    list_user_jobs,
)
from jobpilot.db.models import AiRun, JobPosting, JobStage, RunStatus, UserJob
from jobpilot.db.reports import latest_match_reports_by_job
from jobpilot.db.runs import set_run_status
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.injection import detect_injection
from jobpilot.services.jobs import (
    BasicFields,
    ParsedJob,
    Requirement,
    RequirementType,
    effective_parsed_job,
    parse_job,
)
from jobpilot.services.keywords import normalize_keywords
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    EmptyModelResponseError,
    ModelBudget,
)
from jobpilot.services.matching import JobMatchSummary, summarize_stored_match
from jobpilot.services.parsing import normalize_text

router = APIRouter(prefix="/api", tags=["jobs"])

MAX_JD_CHARS = 20_000
_POSTING_UNIQUE = "uq_job_posting_owner_id"
_PARSE_BUDGET = {"remaining_calls": 3, "remaining_tokens": 40_000}
_MODEL_ERRORS = (
    APIConnectionError,
    APIStatusError,
    BudgetExhaustedError,
    TruncatedOutputError,
    EmptyModelResponseError,
    ValidationError,
)


class JobIn(BaseModel):
    """JD 正文：粘贴的文字，或截图识别后由用户核对过的文字。"""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=MAX_JD_CHARS)


class RequirementIn(BaseModel):
    """用户修改后的一条要求。带原有的 requirement_id 且文字没改时，保留原文片段。"""

    model_config = ConfigDict(extra="forbid")

    requirement_id: UUID | None = None
    type: RequirementType
    text: str = Field(min_length=1, max_length=300)


class JobChangesIn(BaseModel):
    """没给的字段保持不变；requirements、keywords 给了就整体替换。"""

    model_config = ConfigDict(extra="forbid")

    stage: JobStage | None = None
    note: str | None = Field(default=None, max_length=2000)
    basic_fields: BasicFields | None = None
    requirements: list[RequirementIn] | None = Field(default=None, max_length=80)
    keywords: list[str] | None = Field(default=None, max_length=60)

    @field_validator("requirements")
    @classmethod
    def unique_requirement_ids(
        cls, requirements: list[RequirementIn] | None
    ) -> list[RequirementIn] | None:
        seen = set()
        for item in requirements or []:
            if item.requirement_id is None:
                continue
            if item.requirement_id in seen:
                raise ValueError(f"重复的岗位要求 ID：{item.requirement_id}")
            seen.add(item.requirement_id)
        return requirements


class JobSummary(BaseModel):
    job_id: UUID
    title: str | None
    company: str | None
    salary: str | None
    location: str | None
    onsite_days_per_week: str | None
    education: str | None
    stage: JobStage
    keywords: list[str]
    injection_count: int
    match_summary: JobMatchSummary | None
    updated_at: datetime


class JobOut(BaseModel):
    job_id: UUID
    stage: JobStage
    note: str | None
    raw_text: str
    parsed: ParsedJob  # 合并个人修改后的结果，匹配和改写都用它
    overridden: list[str]  # 哪些部分被用户改过
    overridden_basic_fields: list[str]  # 与 AI 原提取不同的基本字段
    injection_flags: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


def _overridden_basic_fields(posting: JobPosting, effective: ParsedJob) -> list[str]:
    extracted = ParsedJob.model_validate(posting.parsed_json).basic_fields
    current = effective.basic_fields
    return sorted(
        name
        for name in BasicFields.model_fields
        if getattr(current, name) != getattr(extracted, name)
    )


def _job_out(posting: JobPosting, user_job: UserJob) -> JobOut:
    parsed = effective_parsed_job(posting, user_job)
    return JobOut(
        job_id=user_job.user_job_id,
        stage=JobStage(user_job.stage),
        note=user_job.note,
        raw_text=posting.raw_text,
        parsed=parsed,
        overridden=sorted(user_job.field_overrides_json),
        overridden_basic_fields=_overridden_basic_fields(posting, parsed),
        injection_flags=posting.injection_flags,
        created_at=user_job.created_at,
        updated_at=user_job.updated_at,
    )


async def _load(
    db: DbSession, user_id: UUID, job_id: UUID, *, for_update: bool = False
) -> tuple[JobPosting, UserJob]:
    row = await get_user_job(db, user_id, job_id, for_update=for_update)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    return row


async def reserve_job(
    body: JobIn,
    user: CurrentUser,
    db: DbSession,
    factory: SessionFactory,
    key: RequiredKey,
) -> AsyncIterator[tuple[JobIn, Attempt]]:
    async with idempotent_request(
        db,
        factory,
        KeyScope(user.user_id, Operation.PARSE_JD, key),
        body.model_dump_json(),
    ) as attempt:
        yield body, attempt


JobSubmission = Annotated[tuple[JobIn, Attempt], Depends(reserve_job, scope="function")]


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def add_job(
    submission: JobSubmission,
    response: Response,
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
) -> JobOut:
    """解析 JD 并加入岗位库。同一用户再次提交同样的正文，直接返回已有的岗位（200）。"""
    body, attempt = submission
    user_id = attempt.scope.user_id
    raw_text = normalize_text(body.raw_text)
    if not raw_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="JD 内容为空"
        )
    content_hash = sha256(raw_text.encode("utf-8")).hexdigest()

    existing = await find_existing_job(db, user_id, content_hash)
    if existing is not None:
        return await _reuse_job(db, response, attempt, existing)

    run = AiRun(user_id=user_id, kind=Operation.PARSE_JD)
    db.add(run)
    await db.commit()
    parsed = await _parse_posting(db, session_factory, client, run, raw_text)
    posting = JobPosting(
        owner_id=user_id,
        raw_text=raw_text,
        content_hash=content_hash,
        parsed_json=parsed.model_dump(mode="json"),
        keywords=parsed.keywords,
        injection_flags=[flag.to_json() for flag in detect_injection(raw_text)],
    )
    return await _store_job(db, response, attempt, run, posting)


async def _parse_posting(
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
    run: AiRun,
    raw_text: str,
) -> ParsedJob:
    try:
        return await parse_job(
            session_factory,
            run.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(**_PARSE_BUDGET),
            raw_text=raw_text,
        )
    except _MODEL_ERRORS:
        await set_run_status(db, run.user_id, run.run_id, RunStatus.FAILED)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="JD 解析失败，请稍后重试"
        ) from None


async def _store_job(
    db: DbSession, response: Response, attempt: Attempt, run: AiRun, posting: JobPosting
) -> JobOut:
    user_id, run_id, content_hash = run.user_id, run.run_id, posting.content_hash
    db.add(posting)
    try:
        await db.flush()
    except IntegrityError as error:
        # 连点两次提交：两个请求都在对方插入前查过重，后插入的撞上唯一约束。
        # 数据库已经保证只存一份，这里把报错换成返回先到的那份。
        await db.rollback()
        if error.orig.diag.constraint_name != _POSTING_UNIQUE:
            raise
        await set_run_status(db, user_id, run_id, RunStatus.CANCELLED)
        existing = await find_existing_job(db, user_id, content_hash)
        if existing is None:
            raise HTTPException(409, "该岗位在导入期间被删除，请重试") from None
        return await _reuse_job(db, response, attempt, existing)
    user_job = UserJob(user_id=user_id, posting_id=posting.posting_id)
    db.add(user_job)
    await db.flush()
    run.user_job_id = user_job.user_job_id
    await finish_product(db, user_id, run_id)
    output = _job_out(posting, user_job)
    await save_snapshot(db, attempt, output.job_id, output, status.HTTP_201_CREATED)
    await db.commit()
    return output


async def _reuse_job(
    db: DbSession, response: Response, attempt: Attempt, job_id: UUID
) -> JobOut:
    # 与删除岗位共用行锁，避免删除事务提交后才补写旧快照。
    output = _job_out(*await _load(db, attempt.scope.user_id, job_id, for_update=True))
    await save_snapshot(db, attempt, job_id, output, status.HTTP_200_OK)
    await db.commit()
    response.status_code = status.HTTP_200_OK
    return output


@router.get("/jobs")
async def list_jobs(user: CurrentUser, db: DbSession) -> list[JobSummary]:
    rows = await list_user_jobs(db, user.user_id)
    latest_reports = await latest_match_reports_by_job(db, user.user_id)
    summaries = []
    for posting, user_job in rows:
        parsed = effective_parsed_job(posting, user_job)
        fields = parsed.basic_fields
        report = latest_reports.get(user_job.user_job_id)
        summaries.append(
            JobSummary(
                job_id=user_job.user_job_id,
                title=fields.title,
                company=fields.company,
                salary=fields.salary,
                location=fields.location,
                onsite_days_per_week=fields.onsite_days_per_week,
                education=fields.education,
                stage=JobStage(user_job.stage),
                keywords=parsed.keywords,
                injection_count=len(posting.injection_flags),
                match_summary=summarize_stored_match(*report) if report else None,
                updated_at=user_job.updated_at,
            )
        )
    return summaries


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, user: CurrentUser, db: DbSession) -> JobOut:
    return _job_out(*await _load(db, user.user_id, job_id))


@router.put("/jobs/{job_id}")
async def update_job(
    job_id: UUID, body: JobChangesIn, user: CurrentUser, db: DbSession
) -> JobOut:
    """修改进度、备注或解析结果。解析结果的修改写进 user_job，job_posting 不变。"""
    posting, user_job = await _load(db, user.user_id, job_id, for_update=True)
    if body.stage is not None:
        user_job.stage = body.stage
    if body.note is not None:
        user_job.note = body.note

    overrides = dict(user_job.field_overrides_json)
    if body.basic_fields is not None:
        fields = effective_parsed_job(posting, user_job).basic_fields
        overrides["basic_fields"] = fields.model_dump(mode="json") | (
            body.basic_fields.model_dump(mode="json", exclude_unset=True)
        )
    if body.requirements is not None:
        current = {
            r.requirement_id: r
            for r in effective_parsed_job(posting, user_job).requirements
        }
        requirements = []
        for item in body.requirements:
            kept = current.get(item.requirement_id) if item.requirement_id else None
            if kept is not None and kept.text == item.text:
                requirement = kept.model_copy(update={"type": item.type})
            else:  # 新加的或改了文字的要求：用户自己的说法，不带原文片段
                requirement = Requirement(type=item.type, text=item.text)
            requirements.append(requirement.model_dump(mode="json"))
        overrides["requirements"] = requirements
    if body.keywords is not None:
        overrides["keywords"] = normalize_keywords(body.keywords)
    user_job.field_overrides_json = overrides  # 整体替换，ORM 才会发现 JSONB 变了

    await db.commit()
    await db.refresh(user_job)
    return _job_out(posting, user_job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(
    job_id: UUID, user: CurrentUser, db: DbSession, launcher: Launcher
) -> None:
    """删除当前用户的岗位记录及其匹配报告、改写运行和定制简历；公共或共用岗位原文不动。"""
    deleted_runs = await delete_user_job(db, user.user_id, job_id)
    if deleted_runs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    await db.commit()
    launcher.cancel_runs(deleted_runs)
