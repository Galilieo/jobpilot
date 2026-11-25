"""通过账号 HTTP 接口与真实 PostgreSQL 验证两阶段删除。"""

from datetime import datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.models import AiRun, RunStatus, User
from jobpilot.tests.test_auth import login, register, sessions_of

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
