"""改写接口：启动时固定底稿与输入、后台执行、查询结果、跨用户 404、段落类型待指定、
程序错误和服务重启后的终态。

数据直接写进数据库，只经由改写接口和登录接口调用，不依赖上传、确认等其他接口。
"""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api import tailoring
from jobpilot.api.deps import get_model_client
from jobpilot.api.tailoring import RunLauncher, get_run_launcher
from jobpilot.db.models import AiRun, ResumeDocument, RunStatus
from jobpilot.db.runs import fail_abandoned_runs
from jobpilot.main import app
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_fact_schema import add_user
from jobpilot.tests.test_tailoring_graph import (
    INTERN,
    PROJECT,
    RAW_TEXT,
    FakeModel,
    seed_inputs,
)

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def launcher(async_client: httpx.AsyncClient) -> AsyncIterator[RunLauncher]:
    """替换后台任务，测试可以等改写跑完再查询。"""
    runner = RunLauncher()
    app.dependency_overrides[get_run_launcher] = lambda: runner
    yield runner
    await runner.wait_all()


async def sign_in(client: httpx.AsyncClient, email: str) -> UUID:
    credentials = {"email": email, "password": "tailor-api-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success
    return UUID((await client.get("/api/me")).json()["user_id"])


@pytest.fixture
async def model(async_client: httpx.AsyncClient) -> AsyncIterator[FakeModel]:
    fake = FakeModel(fact_ids=[])  # 测试写入数据后再填
    async with fake_upstream(fake) as client:
        app.dependency_overrides[get_model_client] = lambda: client
        yield fake


async def start(client: httpx.AsyncClient, job_id: UUID, **body) -> httpx.Response:
    return await client.post(f"/api/jobs/{job_id}/tailor", json=body)


async def add_document(session_factory: Factory, user_id: UUID) -> UUID:
    async with session_factory.begin() as session:
        doc = ResumeDocument(user_id=user_id, raw_text=RAW_TEXT)
        session.add(doc)
        await session.flush()
        return doc.doc_id


# ---------- 启动与查询 ----------


async def test_start_runs_in_background_and_result_reads_back(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "run@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids

    started = await start(async_client, inputs.user_job_id)
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    await launcher.wait_all()

    result = (await async_client.get(f"/api/tailor-runs/{run_id}")).json()
    assert (result["status"], result["stop_reason"]) == ("succeeded", None)
    assert result["base_doc_id"] == str(inputs.doc_id)
    assert result["job_id"] == str(inputs.user_job_id)
    assert [b["status"] for b in result["blocks"]] == ["done", "done", "done"]
    assert [(b["experience"], b["check_status"]) for b in result["bullets"]] == [
        (INTERN, "passed"),
        (PROJECT, "passed"),
        (None, "passed"),
    ]
    assert len(result["bullets"][0]["fact_ids"]) == 2


async def test_base_doc_defaults_to_latest_and_stays_fixed(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """06 T2.4 验收：上传 A 再上传 B，默认选 B；指定 A 选 A；
    启动后再上传 C，两个运行的底稿都不变。"""
    user_id = await sign_in(async_client, "base@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)  # 文档 A
    doc_b = await add_document(session_factory, user_id)

    default = (await start(async_client, inputs.user_job_id)).json()
    chosen = (
        await start(async_client, inputs.user_job_id, base_doc_id=str(inputs.doc_id))
    ).json()
    assert default["base_doc_id"] == str(doc_b)
    assert chosen["base_doc_id"] == str(inputs.doc_id)

    await add_document(session_factory, user_id)  # 文档 C
    await launcher.wait_all()
    for run, doc in ((default, doc_b), (chosen, inputs.doc_id)):
        result = (await async_client.get(f"/api/tailor-runs/{run['run_id']}")).json()
        assert result["base_doc_id"] == str(doc)


async def test_preconditions_are_checked_before_creating_a_run(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "pre@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id, sections=False)

    missing = await start(async_client, inputs.user_job_id)
    assert missing.status_code == 422
    assert set(missing.json()["detail"]["experiences"]) == {INTERN, PROJECT, "技能"}

    unknown_job = await start(async_client, UUID(int=1))
    assert unknown_job.status_code == 404
    async with session_factory() as session:
        runs = await session.scalar(
            select(func.count()).where(AiRun.user_id == user_id, AiRun.kind == "tailor")
        )
    assert runs == 0


async def test_user_given_sections_unblock_legacy_facts(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "legacy@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id, sections=False)
    model.fact_ids = inputs.fact_ids
    sections = {INTERN: "internship", PROJECT: "project", "技能": "skill"}

    started = await start(async_client, inputs.user_job_id, sections=sections)
    assert started.status_code == 202, started.text
    await launcher.wait_all()
    result = (
        await async_client.get(f"/api/tailor-runs/{started.json()['run_id']}")
    ).json()
    assert result["status"] == "succeeded"
    assert [b["experience"] for b in result["blocks"]] == [INTERN, PROJECT, None]


# ---------- 隔离 ----------


async def test_other_users_job_doc_and_run_are_404(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    owner = await sign_in(async_client, "owner@tailor-api.invalid")
    theirs = await seed_inputs(session_factory, owner)
    model.fact_ids = theirs.fact_ids
    run_id = (await start(async_client, theirs.user_job_id)).json()["run_id"]
    await launcher.wait_all()

    other = await sign_in(async_client, "other@tailor-api.invalid")
    mine = await seed_inputs(session_factory, other)
    assert (await async_client.get(f"/api/tailor-runs/{run_id}")).status_code == 404
    assert (await start(async_client, theirs.user_job_id)).status_code == 404
    with_their_doc = await start(
        async_client, mine.user_job_id, base_doc_id=str(theirs.doc_id)
    )
    assert with_their_doc.status_code == 404


# ---------- 终态 ----------


async def test_program_error_in_background_marks_run_failed(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(tailoring, "run_tailoring", broken)
    user_id = await sign_in(async_client, "bug@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)

    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()
    result = (await async_client.get(f"/api/tailor-runs/{run_id}")).json()
    assert result["status"] == "failed" and result["finished_at"] is not None


async def test_restart_fails_running_runs_but_keeps_waiting_ones(
    session_factory: Factory,
) -> None:
    user_id = await add_user(session_factory, "restart@tailor-api.invalid")
    async with session_factory.begin() as session:
        running = AiRun(user_id=user_id, kind="tailor")
        waiting = AiRun(user_id=user_id, kind="agent", status="waiting_confirmation")
        session.add_all([running, waiting])
    async with session_factory.begin() as session:
        assert await fail_abandoned_runs(session) == 1

    async with session_factory() as session:
        running = await session.get_one(AiRun, running.run_id)
        waiting = await session.get_one(AiRun, waiting.run_id)
    assert (running.status, running.stop_reason) == ("failed", "service_restarted")
    assert running.finished_at is not None
    assert waiting.status == RunStatus.WAITING_CONFIRMATION
