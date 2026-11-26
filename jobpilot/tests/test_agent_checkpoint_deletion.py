"""使用真实 Postgres 检查点验证删除对话和账号时的线程清理。"""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.db.checkpoints import checkpoint_saver
from jobpilot.db.conversations import conversation_thread
from jobpilot.main import app
from jobpilot.services.accounts import PurgeResult, purge_accounts
from jobpilot.tests.test_account_archive import _expire
from jobpilot.tests.test_agent_api import batch, card_of, conversation, sign_in
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_graph import Factory


async def _wait_for_card(
    client: httpx.AsyncClient, email: str
) -> tuple[UUID, str, str]:
    await sign_in(client, email)
    user_id = UUID((await client.get("/api/me")).json()["user_id"])
    path = await conversation(client)
    card = card_of(await client.post(f"{path}/messages", json={"content": "请问情况"}))
    assert card["kind"] == "question"
    return user_id, path, conversation_thread(user_id, UUID(path.split("/")[-1]))


async def _checkpoint_counts(factory: Factory, thread_id: str) -> list[int]:
    async with factory() as session:
        return [
            await session.scalar(
                text(
                    f"SELECT count(*) FROM jobpilot_checkpoint.{table} "
                    "WHERE thread_id = :thread_id"
                ),
                {"thread_id": thread_id},
            )
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        ]


@pytest.mark.parametrize(
    "delete_account", [False, True], ids=["conversation", "account"]
)
async def test_checkpoint_deletion_keeps_other_user_thread(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    delete_account: bool,
) -> None:
    email = "checkpoint-purge@example.invalid"
    async with fake_upstream(
        lambda _: batch(("ask_user", {"questions": ["何时到岗"]}))
    ) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        _, _, control = await _wait_for_card(
            async_client, "checkpoint-control@example.invalid"
        )
        user_id, path, thread_id = await _wait_for_card(async_client, email)
    before = await _checkpoint_counts(session_factory, thread_id)
    assert all(count > 0 for count in before)
    archive = (await async_client.get("/api/account/export")).json()
    assert len(archive["conversations"]) == len(archive["agent_actions"]) == 1
    assert len(archive["messages"]) == 2

    if delete_account:
        assert (await async_client.delete("/api/account")).status_code == 204
        await _expire(session_factory, user_id)
        assert await purge_accounts(session_factory) == PurgeResult(deleted=1, failed=0)
    else:
        assert (await async_client.delete(path)).status_code == 204

    assert await _checkpoint_counts(session_factory, thread_id) == [0, 0, 0]
    async with checkpoint_saver(settings.test_database_url) as saver:
        assert (
            await saver.aget_tuple({"configurable": {"thread_id": control}}) is not None
        )
