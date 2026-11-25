"""通过账号 HTTP 接口与真实 PostgreSQL 验证两阶段删除。"""

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobpilot import main
from jobpilot.api import account, tailoring
from jobpilot.api.deps import get_model_client
from jobpilot.api.tailoring import RunLauncher, get_run_launcher
from jobpilot.config import settings
from jobpilot.db.models import AiRun, RunStatus, User
from jobpilot.main import app
from jobpilot.services import accounts
from jobpilot.services.tailoring_graph import TailorDeps
from jobpilot.tests.test_auth import login, register, sessions_of
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_api import start
from jobpilot.tests.test_tailoring_graph import Factory, FakeModel, seed_inputs

EMAIL = "lifecycle@account.invalid"


async def _sign_in(client: httpx.AsyncClient, email: str = EMAIL) -> UUID:
    created = await register(client, email)
    assert created.status_code == 201
    assert (await login(client, email)).status_code == 204
    return UUID(created.json()["user_id"])


async def test_delete_revokes_every_session_and_cancels_only_own_running_runs(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _sign_in(async_client)
    first_token = async_client.cookies[settings.cookie_name]
    other_id = await _sign_in(async_client, "other@account.invalid")
    assert (await login(async_client, EMAIL)).status_code == 204
    async with session_factory.begin() as db:
        db.add_all(
            [
                AiRun(user_id=user_id, kind="tailor"),
                AiRun(
                    user_id=user_id,
                    kind="tailor",
                    status=RunStatus.WAITING_CONFIRMATION,
                ),
                AiRun(user_id=other_id, kind="tailor"),
            ]
        )

    assert (await async_client.delete("/api/account")).status_code == 204
    assert settings.cookie_name not in async_client.cookies
    async_client.cookies.set(settings.cookie_name, first_token)
    assert (await async_client.get("/api/me")).status_code == 401
    assert await sessions_of(session_factory, EMAIL) == []
    assert len(await sessions_of(session_factory, "other@account.invalid")) == 1
    async with session_factory() as db:
        runs = list((await db.scalars(select(AiRun))).all())
    assert {(run.user_id, run.status) for run in runs} == {
        (user_id, RunStatus.CANCELLED),
        (other_id, RunStatus.RUNNING),
    }
    assert all(
        (run.finished_at is not None) == (run.user_id == user_id) for run in runs
    )


async def test_can_restore_until_deadline(async_client: httpx.AsyncClient) -> None:
    await _sign_in(async_client)
    assert (await async_client.delete("/api/account")).status_code == 204
    assert (await login(async_client, EMAIL)).status_code == 204
    pending = (await async_client.get("/api/me")).json()
    requested = datetime.fromisoformat(pending["deletion_requested_at"])
    assert datetime.fromisoformat(
        pending["deletion_deadline"]
    ) - requested == timedelta(days=7)
    assert (await async_client.delete("/api/account")).status_code == 403
    assert (await async_client.get("/api/me")).json() == pending
    assert (await async_client.post("/api/account/restore")).status_code == 204
    restored = (await async_client.get("/api/me")).json()
    assert restored["deletion_requested_at"] is restored["deletion_deadline"] is None
    assert (await async_client.get("/api/jobs")).status_code == 200


async def test_restore_rejects_expired_account(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _sign_in(async_client)
    async with session_factory.begin() as db:
        await db.execute(
            update(User)
            .where(User.user_id == user_id)
            .values(
                deletion_requested_at=func.now() - timedelta(days=7),
            )
        )

    response = await async_client.post("/api/account/restore")

    assert response.status_code == 409
    assert "恢复期" in response.json()["detail"]
    assert (await async_client.get("/api/me")).json()["deletion_deadline"]


@pytest.mark.parametrize("scenario", ["late-result", "late-match", "task-cancelled"])
async def test_cancelled_tailoring_cannot_write_late_results(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    user_id = await _sign_in(async_client)
    inputs = await seed_inputs(session_factory, user_id)
    launcher = RunLauncher()
    app.dependency_overrides[get_run_launcher] = lambda: launcher
    fake = FakeModel(inputs.fact_ids)
    paused = asyncio.Event()
    resume = asyncio.Event()
    actual_graph = tailoring.run_tailoring
    pause_before_graph = scenario != "late-result"
    if scenario != "task-cancelled":
        # 即便旧任务已继续执行，数据库仍须拒绝写入结果或启动新的运行。
        monkeypatch.setattr(launcher, "cancel_runs", lambda _: None)

    async def pause_graph(
        deps: TailorDeps, user_id: UUID, run_id: UUID, **kwargs
    ) -> None:
        paused.set()
        await resume.wait()
        await actual_graph(deps, user_id, run_id, **kwargs)

    async def respond(request: httpx.Request) -> httpx.Response:
        reply = fake(request)
        if not pause_before_graph and fake.calls["rewrite"]:
            paused.set()
            await resume.wait()
        return reply

    if pause_before_graph:
        monkeypatch.setattr(tailoring, "run_tailoring", pause_graph)
    async with fake_upstream(respond) as model_client:
        app.dependency_overrides[get_model_client] = lambda: model_client
        result = await start(async_client, inputs.user_job_id)
        assert result.status_code == 202, result.text
        try:
            await asyncio.wait_for(paused.wait(), timeout=5)
            assert (await async_client.delete("/api/account")).status_code == 204
            if scenario != "late-match":
                # 恢复只放行业务入口，已取消的运行仍不能提交。
                await _restore_through_api(async_client)
        finally:
            resume.set()
            await launcher.wait_all()
    if pause_before_graph:
        assert fake.calls["match"] == 0
    if scenario == "late-match":
        await _restore_through_api(async_client)
    run_url = f"/api/tailor-runs/{result.json()['run_id']}"
    run = (await async_client.get(run_url)).json()
    assert run["status"] == "cancelled" and run["bullets"] == []
    assert (await async_client.get(f"{run_url}/resume")).status_code == 404


async def _restore_through_api(client: httpx.AsyncClient) -> None:
    assert (await login(client, EMAIL)).status_code == 204
    assert (await client.post("/api/account/restore")).status_code == 204


async def test_purge_keeps_recent_accounts_and_retries_only_failed_deletes(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = await _sign_in(async_client)
    healthy = await _sign_in(async_client, "another-due@account.invalid")
    recent = await _sign_in(async_client, "recent@account.invalid")
    async with session_factory.begin() as db:
        await db.execute(
            update(User)
            .where(User.user_id.in_((failing, healthy)))
            .values(
                deletion_requested_at=func.now() - timedelta(days=8),
            )
        )
        await db.execute(
            update(User)
            .where(User.user_id == recent)
            .values(
                deletion_requested_at=func.now() - timedelta(days=6),
            )
        )

    delete_due_account = accounts.delete_due_account

    async def interrupted(db: AsyncSession, user_id: UUID) -> bool:
        removed = await delete_due_account(db, user_id)
        if user_id == failing:
            raise SQLAlchemyError("模拟提交前故障")
        return removed

    with monkeypatch.context() as patch:
        patch.setattr(accounts, "delete_due_account", interrupted)
        assert await accounts.purge_accounts(session_factory) == accounts.PurgeResult(
            1, 1
        )
    assert (await login(async_client, EMAIL)).status_code == 204
    assert await accounts.purge_accounts(session_factory) == accounts.PurgeResult(1, 0)
    assert (await login(async_client, EMAIL)).status_code == 401
    assert (await login(async_client, "recent@account.invalid")).status_code == 204


async def test_cleanup_runs_at_startup_and_then_repeats(
    async_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_id = await _sign_in(async_client)
    repeated_id = await _sign_in(async_client, "daily@account.invalid")
    await _set_request_to_past(session_factory, startup_id)
    monkeypatch.setattr(
        main, "create_database_engine", lambda *args, **kwargs: db_engine
    )
    monkeypatch.setattr(accounts, "_PURGE_INTERVAL_SECONDS", 0.02)

    async with main.lifespan(FastAPI()):
        assert not await _account_exists(session_factory, startup_id)
        assert await _account_exists(session_factory, repeated_id)
        await _set_request_to_past(session_factory, repeated_id)
        async with asyncio.timeout(3):
            while await _account_exists(session_factory, repeated_id):
                await asyncio.sleep(0.02)


async def _account_exists(factory: Factory, user_id: UUID) -> bool:
    async with factory() as session:
        return await session.get(User, user_id) is not None


async def _set_request_to_past(factory: Factory, user_id: UUID) -> None:
    async with factory.begin() as session:
        await session.execute(
            update(User)
            .where(User.user_id == user_id)
            .values(
                deletion_requested_at=func.now() - timedelta(days=8),
            )
        )


async def test_concurrent_retry_preserves_the_first_deletion_request(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _sign_in(async_client)
    first_waiting = asyncio.Event()
    second_waiting = asyncio.Event()
    release_second = asyncio.Event()
    request_deletion = account.request_account_deletion

    async def synchronize_deletion(*args):
        if first_waiting.is_set():
            second_waiting.set()
            await release_second.wait()
        else:
            first_waiting.set()
            await second_waiting.wait()
        return await request_deletion(*args)

    monkeypatch.setattr(account, "request_account_deletion", synchronize_deletion)
    async with asyncio.timeout(5), asyncio.TaskGroup() as requests:
        first = requests.create_task(async_client.delete("/api/account"))
        await first_waiting.wait()
        retry = requests.create_task(async_client.delete("/api/account"))
        assert (await first).status_code == 204
        assert (await login(async_client, EMAIL)).status_code == 204
        pending = (await async_client.get("/api/me")).json()
        assert pending["deletion_requested_at"]
        release_second.set()
        assert (await retry).status_code == 204
    assert (await login(async_client, EMAIL)).status_code == 204
    assert (await async_client.get("/api/me")).json() == pending
