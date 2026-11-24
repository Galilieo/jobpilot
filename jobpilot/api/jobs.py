"""岗位库接口：提交 JD 并解析、列表、详情、修改。

job_posting 存原文和解析结果，不随个人修改而变；个人的进度、备注和对解析结果的修改
写在 user_job。路径里的 job_id 指 user_job，不是本人的一律 404。
本周 MVP 不做薪资换算、筛选、统计和自定义字段（见 06 执行进度）。
"""

from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jobpilot.api.deps import CurrentUser, DbSession, ModelClient, SessionFactory
from jobpilot.db.models import AiRun, JobPosting, JobStage, RunStatus, UserJob
from jobpilot.db.runs import set_run_status
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.injection import detect_injection
from jobpilot.services.jobs import (
    BasicFields,
    ParsedJob,
    Requirement,
    RequirementType,
    effective_parsed_job,
    get_user_job,
    parse_job,
)
from jobpilot.services.keywords import normalize_keywords
from jobpilot.services.llm_gateway import BudgetExhaustedError, ModelBudget
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


class JobSummary(BaseModel):
    job_id: UUID
    title: str | None
    company: str | None
    stage: JobStage
    keywords: list[str]
    injection_count: int
    updated_at: datetime


class JobOut(BaseModel):
    job_id: UUID
    stage: JobStage
    note: str | None
    raw_text: str
    parsed: ParsedJob  # 合并个人修改后的结果，匹配和改写都用它
    overridden: list[str]  # 哪些部分被用户改过
    injection_flags: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


def _job_out(posting: JobPosting, user_job: UserJob) -> JobOut:
    return JobOut(
        job_id=user_job.user_job_id,
        stage=JobStage(user_job.stage),
        note=user_job.note,
        raw_text=posting.raw_text,
        parsed=effective_parsed_job(posting, user_job),
        overridden=sorted(user_job.field_overrides_json),
        injection_flags=posting.injection_flags,
        created_at=user_job.created_at,
        updated_at=user_job.updated_at,
    )


async def _load(
    db: DbSession, user_id: UUID, job_id: UUID
) -> tuple[JobPosting, UserJob]:
    row = await get_user_job(db, user_id, job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    return row


async def _find_existing(
    db: DbSession, user_id: UUID, content_hash: str
) -> UUID | None:
    """本人以前提交过同样正文的岗位；按正文指纹查，只查自己的。"""
    return await db.scalar(
        select(UserJob.user_job_id)
        .join(
            JobPosting,
            (UserJob.posting_id == JobPosting.posting_id)
            & (UserJob.user_id == JobPosting.owner_id),
        )
        .where(JobPosting.owner_id == user_id, JobPosting.content_hash == content_hash)
    )


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def add_job(
    body: JobIn,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
) -> JobOut:
    """解析 JD 并加入岗位库。同一用户再次提交同样的正文，直接返回已有的岗位（200）。"""
    raw_text = normalize_text(body.raw_text)
    if not raw_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="JD 内容为空"
        )
    content_hash = sha256(raw_text.encode("utf-8")).hexdigest()

    existing = await _find_existing(db, user.user_id, content_hash)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _job_out(*await _load(db, user.user_id, existing))

    run = AiRun(user_id=user.user_id, kind="parse_jd")
    db.add(run)
    await db.commit()
    # flush 失败后 session 处于待回滚状态，回滚又会让对象过期；之后再读 user.user_id
    # 都会触发异步里不允许的隐式加载。所以趁现在把要用的 ID 取出来。
    user_id, run_id = user.user_id, run.run_id
    try:
        parsed = await parse_job(
            session_factory,
            user.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(**_PARSE_BUDGET),
            raw_text=raw_text,
        )
    except _MODEL_ERRORS:
        await set_run_status(db, user.user_id, run.run_id, RunStatus.FAILED)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="JD 解析失败，请稍后重试"
        ) from None

    posting = JobPosting(
        owner_id=user.user_id,
        raw_text=raw_text,
        content_hash=content_hash,
        parsed_json=parsed.model_dump(mode="json"),
        keywords=parsed.keywords,
        injection_flags=[flag.to_json() for flag in detect_injection(raw_text)],
    )
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
        await db.commit()
        existing = await _find_existing(db, user_id, content_hash)
        assert existing is not None
        response.status_code = status.HTTP_200_OK
        return _job_out(*await _load(db, user_id, existing))
    user_job = UserJob(user_id=user.user_id, posting_id=posting.posting_id)
    db.add(user_job)
    await db.flush()
    run.user_job_id = user_job.user_job_id
    await set_run_status(db, user.user_id, run.run_id, RunStatus.SUCCEEDED)
    await db.commit()
    return _job_out(posting, user_job)


@router.get("/jobs")
async def list_jobs(user: CurrentUser, db: DbSession) -> list[JobSummary]:
    rows = await db.execute(
        select(JobPosting, UserJob)
        .join(
            UserJob,
            (UserJob.posting_id == JobPosting.posting_id)
            & (UserJob.user_id == JobPosting.owner_id),
        )
        .where(UserJob.user_id == user.user_id)
        .order_by(UserJob.updated_at.desc())
    )
    summaries = []
    for posting, user_job in rows:
        parsed = effective_parsed_job(posting, user_job)
        summaries.append(
            JobSummary(
                job_id=user_job.user_job_id,
                title=parsed.basic_fields.title,
                company=parsed.basic_fields.company,
                stage=JobStage(user_job.stage),
                keywords=parsed.keywords,
                injection_count=len(posting.injection_flags),
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
    posting, user_job = await _load(db, user.user_id, job_id)
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
