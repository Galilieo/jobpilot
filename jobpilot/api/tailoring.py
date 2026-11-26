"""改写接口：启动改写运行、查看运行状态与结果（02 第 6 节，06 T2.4 第四步）。

启动时把底稿、事实库版本和岗位快照固定在 ai_run，之后上传新简历、发布新事实、改岗位
都不影响这次运行。改写在进程内的后台任务里执行，接口立即返回 run_id。
查询只读业务表（ai_run、tailored_bullet、bullet_fact），不读 LangGraph 检查点。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Coroutine, Iterable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg.errors import CheckViolation
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api.deps import CurrentUser, DbSession, ModelClient, SessionFactory
from jobpilot.api.idempotency import RequiredKey, idempotent_request, save_snapshot
from jobpilot.db.idempotency import Attempt, KeyScope, Operation
from jobpilot.db.jobs import get_user_job
from jobpilot.db.models import (
    AiRun,
    BulletFact,
    ResumeDocument,
    RunStatus,
    SectionKind,
    TailoredBullet,
    TailoredResume,
)
from jobpilot.db.runs import RunAlreadyFinishedError, set_run_status
from jobpilot.diagnostics import describe_error, error_locations
from jobpilot.services import drafts
from jobpilot.services.jobs import effective_parsed_job
from jobpilot.services.llm_gateway import ModelBudget
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

logger = logging.getLogger("uvicorn.error").getChild("tailoring")
router = APIRouter(prefix="/api", tags=["tailoring"])

# 一次改写运行的额度。先按估算设：5 块 × (1 次改写 + 4 条第三层 + 约 2 次修订和复查)
# 再加匹配，约 35 次；E5 测出中位数后改成它的 2～3 倍（02 第 3.2 节）。
_TAILOR_BUDGET = {"remaining_calls": 60, "remaining_tokens": 200_000}
# E5 runner 从这里读取接口预算与并发配置。
_MAX_CONCURRENT_REQUESTS = TAILOR_MAX_CONCURRENT_REQUESTS


class RunLauncher:
    """进程内后台任务。持有任务引用，避免任务还没跑完就被垃圾回收；测试可以等它们结束。"""

    def __init__(self) -> None:
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def launch(self, run_id: UUID, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._finish(run_id, done))

    def _finish(self, run_id: UUID, task: asyncio.Task[None]) -> None:
        """收走终态写入等未处理错误，避免 asyncio 输出原异常。"""
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "后台运行 %s 退出（%s），调用栈：%s",
                run_id,
                describe_error(error),
                error_locations(error),
            )

    def cancel_runs(self, run_ids: Iterable[UUID]) -> None:
        """提交账号删除后停止已取消的进程内任务，避免恢复后旧任务继续派生步骤。"""
        for run_id in run_ids:
            if task := self._tasks.get(run_id):
                task.cancel()

    async def wait_all(self) -> None:
        if self._tasks:
            done, _ = await asyncio.wait(tuple(self._tasks.values()))
            for task in done:
                if not task.cancelled():
                    task.result()


def get_run_launcher(request: Request) -> RunLauncher:
    return request.app.state.run_launcher


Launcher = Annotated[RunLauncher, Depends(get_run_launcher)]


# ---------- 启动 ----------


class TailorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 不填时用最近上传的简历。
    base_doc_id: UUID | None = None
    # 推断不出段落类型的经历，由用户指定：{经历名: 段落类型}。
    sections: dict[str, SectionKind] = Field(default_factory=dict)


class TailorStartOut(BaseModel):
    run_id: UUID
    base_doc_id: UUID


async def reserve_tailor(
    job_id: UUID,
    body: TailorIn,
    user: CurrentUser,
    db: DbSession,
    factory: SessionFactory,
    key: RequiredKey,
) -> AsyncIterator[tuple[TailorIn, Attempt]]:
    """领取幂等键：同一键、同样的岗位和请求体，重送时返回原来的 run_id。"""
    payload = json.dumps(
        {"job_id": str(job_id), **body.model_dump(mode="json")}, sort_keys=True
    )
    scope = KeyScope(user.user_id, Operation.TAILOR, key)
    async with idempotent_request(db, factory, scope, payload) as attempt:
        yield body, attempt


TailorSubmission = Annotated[
    tuple[TailorIn, Attempt], Depends(reserve_tailor, scope="function")
]


@router.post("/jobs/{job_id}/tailor", status_code=status.HTTP_202_ACCEPTED)
async def start_tailoring(
    job_id: UUID,
    submission: TailorSubmission,
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
    launcher: Launcher,
) -> TailorStartOut:
    """固定输入、创建运行并在后台执行，立即返回 run_id。

    运行和幂等键的成功响应在同一个事务提交；之后才启动后台任务。
    """
    body, attempt = submission
    job_row = await get_user_job(db, user.user_id, job_id)
    if job_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    posting, user_job = job_row
    snapshot = effective_parsed_job(posting, user_job)
    if not snapshot.requirements:
        raise _unprocessable("当前岗位没有可匹配的要求")

    base_doc_id = await _base_doc_id(db, user.user_id, body.base_doc_id)
    fact_set = await drafts.latest_fact_set(db, user.user_id)
    facts = await drafts.facts_of(db, fact_set)
    if fact_set is None or not facts:
        raise _unprocessable("尚未发布可用的经历事实，请先确认并发布事实库")
    try:
        resolve_sections([TailorFact.of(fact) for fact in facts], body.sections)
    except UnresolvedSectionsError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "有经历无法确定属于哪一栏，请指定",
                "experiences": list(error.experiences),
            },
        ) from None

    user_id = user.user_id
    run_id = uuid4()  # 先定好，响应快照和运行一起提交
    run = AiRun(
        run_id=run_id,
        user_id=user_id,
        kind="tailor",
        user_job_id=user_job.user_job_id,
        fact_set_id=fact_set.fact_set_id,
        base_doc_id=base_doc_id,
        job_snapshot_json=snapshot.model_dump(mode="json"),
    )
    started = TailorStartOut(run_id=run_id, base_doc_id=base_doc_id)
    db.add(run)
    await save_snapshot(db, attempt, run_id, started, status.HTTP_202_ACCEPTED)
    await db.commit()  # 网关登记调用前运行必须已提交

    deps = TailorDeps(
        session_factory=session_factory,
        client=client,
        budget=ModelBudget(**_TAILOR_BUDGET),
        limiter=asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS),
    )
    launcher.launch(run_id, execute(deps, user_id, run_id, body.sections))
    return started


async def _base_doc_id(db: AsyncSession, user_id: UUID, requested: UUID | None) -> UUID:
    """指定了就核对归属（不是本人的一律 404）；没指定用最近上传的一份。"""
    query = select(ResumeDocument.doc_id).where(ResumeDocument.user_id == user_id)
    if requested is not None:
        doc_id = await db.scalar(query.where(ResumeDocument.doc_id == requested))
        if doc_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在"
            )
        return doc_id
    latest = await db.scalar(query.order_by(ResumeDocument.created_at.desc()).limit(1))
    if latest is None:
        raise _unprocessable("还没有上传简历")
    return latest


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
    )


async def execute(
    deps: TailorDeps,
    user_id: UUID,
    run_id: UUID,
    sections: dict[str, SectionKind],
) -> None:
    """后台执行一次改写。没有调用方接异常，所以程序错误也要在这里给运行一个终态。"""
    try:
        await run_tailoring(deps, user_id, run_id, section_overrides=sections)
    except RunAlreadyFinishedError:
        return  # 运行已被别处结束（例如重启回收），结果不写入
    except IntegrityError as error:
        # 账号删号后，后台图可能才开始派生匹配运行。数据库已阻止写入。
        if (
            isinstance(error.orig, CheckViolation)
            and error.orig.diag.constraint_name == "ck_ai_run_account_active"
        ):
            return
        await _fail_execution(deps, user_id, run_id, error)
    except Exception as error:  # noqa: BLE001 — 后台任务里的任何错误都不能让运行停在 running
        await _fail_execution(deps, user_id, run_id, error)


async def _fail_execution(
    deps: TailorDeps, user_id: UUID, run_id: UUID, error: BaseException
) -> None:
    logger.error(
        "改写运行 %s 出错（%s），调用栈：%s",
        run_id,
        describe_error(error),
        error_locations(error),
    )
    await mark_failed(deps.session_factory, user_id, run_id)


async def mark_failed(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID, run_id: UUID
) -> None:
    async with session_factory.begin() as session:
        await set_run_status(session, user_id, run_id, RunStatus.FAILED)


# ---------- 查询 ----------


class BulletOut(BaseModel):
    bullet_id: UUID
    section: str
    experience: str | None
    order_no: int
    text: str
    check_status: str
    revision_count: int
    issues: list[dict[str, Any]]
    fact_ids: list[UUID]


class TailorRunOut(BaseModel):
    run_id: UUID
    job_id: UUID | None
    base_doc_id: UUID | None
    fact_set_id: UUID | None
    status: RunStatus
    stop_reason: str | None
    started_at: datetime
    finished_at: datetime | None
    # 计划里的每块和它的结果；运行结束前为空。
    blocks: list[dict[str, Any]]
    bullets: list[BulletOut]


class TailorRunSummary(BaseModel):
    run_id: UUID
    status: RunStatus
    stop_reason: str | None
    started_at: datetime
    finished_at: datetime | None
    has_resume: bool  # 成功的运行才有完整简历


@router.get("/jobs/{job_id}/tailor-runs")
async def list_tailor_runs(
    job_id: UUID, user: CurrentUser, db: DbSession
) -> list[TailorRunSummary]:
    """这个岗位的改写运行，最新的在前。岗位不是本人的返回 404。"""
    if await get_user_job(db, user.user_id, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    rows = await db.execute(
        select(AiRun, TailoredResume.resume_id)
        .outerjoin(
            TailoredResume,
            (TailoredResume.run_id == AiRun.run_id)
            & (TailoredResume.user_id == AiRun.user_id),
        )
        .where(
            AiRun.user_id == user.user_id,
            AiRun.user_job_id == job_id,
            AiRun.kind == "tailor",
        )
        .order_by(AiRun.started_at.desc())
    )
    return [
        TailorRunSummary(
            run_id=run.run_id,
            status=RunStatus(run.status),
            stop_reason=run.stop_reason,
            started_at=run.started_at,
            finished_at=run.finished_at,
            has_resume=resume_id is not None,
        )
        for run, resume_id in rows.all()
    ]


@router.get("/tailor-runs/{run_id}")
async def get_tailor_run(
    run_id: UUID, user: CurrentUser, db: DbSession
) -> TailorRunOut:
    """运行状态与结果。预算用完时也返回已定稿的条目、每块的结果和停止原因。"""
    run = await db.scalar(
        select(AiRun).where(
            AiRun.run_id == run_id,
            AiRun.user_id == user.user_id,
            AiRun.kind == "tailor",
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行不存在")
    bullets = (
        await db.scalars(
            select(TailoredBullet)
            .where(
                TailoredBullet.run_id == run_id,
                TailoredBullet.user_id == user.user_id,
            )
            .order_by(TailoredBullet.order_no)
        )
    ).all()
    refs = (
        await db.execute(
            select(BulletFact.bullet_id, BulletFact.fact_id).where(
                BulletFact.bullet_id.in_([b.bullet_id for b in bullets]),
                BulletFact.user_id == user.user_id,
            )
        )
    ).all()
    fact_ids: dict[UUID, list[UUID]] = {}
    for bullet_id, fact_id in refs:
        fact_ids.setdefault(bullet_id, []).append(fact_id)

    return TailorRunOut(
        run_id=run.run_id,
        job_id=run.user_job_id,
        base_doc_id=run.base_doc_id,
        fact_set_id=run.fact_set_id,
        status=RunStatus(run.status),
        stop_reason=run.stop_reason,
        started_at=run.started_at,
        finished_at=run.finished_at,
        blocks=(run.plan_json or {}).get("blocks", []),
        bullets=[
            BulletOut(
                bullet_id=b.bullet_id,
                section=b.section,
                experience=b.experience,
                order_no=b.order_no,
                text=b.text,
                check_status=b.check_status,
                revision_count=b.revision_count,
                issues=b.issues_json,
                fact_ids=fact_ids.get(b.bullet_id, []),
            )
            for b in bullets
        ],
    )
