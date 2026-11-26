"""通过真实 HTTP 和 PostgreSQL 验证对话及账号导出的归属。"""

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from jobpilot.db.models import AiRun, Conversation, RunStatus
from jobpilot.tests.test_auth import login, register
from jobpilot.tests.test_fact_schema import Factory, add_user


async def test_conversation_lifecycle_is_visible_in_account_export(
    async_client: httpx.AsyncClient,
) -> None:
    email = "conversation@example.invalid"
    assert (await register(async_client, email)).is_success
    assert (await login(async_client, email)).is_success

    response = await async_client.post("/api/conversations", json={"title": "实习准备"})
    assert response.status_code == 201
    conversation_id = response.json()["conversation_id"]
    path = f"/api/conversations/{conversation_id}"

    assert (await async_client.put(path, json={"title": "后端实习"})).is_success
    detail = (await async_client.get(path)).json()
    assert detail["title"] == "后端实习"
    assert detail["messages"] == []
    assert (await async_client.get("/api/conversations")).json()[0][
        "conversation_id"
    ] == conversation_id

    archive = (await async_client.get("/api/account/export")).json()
    assert archive["conversations"][0]["conversation_id"] == conversation_id
    assert archive["messages"] == archive["agent_actions"] == []

    assert (await async_client.delete(path)).status_code == 204
    assert (await async_client.get(path)).status_code == 404


async def test_postgres_allows_only_one_active_agent_run_per_conversation(
    session_factory: Factory,
) -> None:
    user_id = await add_user(session_factory, "agent-run-limit@example.invalid")
    async with session_factory.begin() as session:
        first = Conversation(user_id=user_id, title="第一段对话")
        second = Conversation(user_id=user_id, title="另一段对话")
        session.add_all([first, second])
        await session.flush()
        session.add(
            AiRun(
                user_id=user_id,
                conversation_id=first.conversation_id,
                kind="agent",
                status=RunStatus.WAITING_CONFIRMATION,
            )
        )

    with pytest.raises(IntegrityError) as failure:
        async with session_factory.begin() as session:
            session.add(
                AiRun(
                    user_id=user_id, conversation_id=first.conversation_id, kind="agent"
                )
            )
    assert failure.value.orig.diag.constraint_name == "uq_ai_run_active_conversation"

    async with session_factory.begin() as session:
        session.add(
            AiRun(user_id=user_id, conversation_id=second.conversation_id, kind="agent")
        )


async def test_postgres_rejects_run_linked_to_another_users_conversation(
    session_factory: Factory,
) -> None:
    owner = await add_user(session_factory, "agent-owner@example.invalid")
    other = await add_user(session_factory, "agent-other@example.invalid")
    async with session_factory.begin() as session:
        conversation = Conversation(user_id=other, title="他人的对话")
        session.add(conversation)

    with pytest.raises(IntegrityError) as failure:
        async with session_factory.begin() as session:
            session.add(
                AiRun(
                    user_id=owner,
                    conversation_id=conversation.conversation_id,
                    kind="agent",
                )
            )
    assert (
        failure.value.orig.diag.constraint_name
        == "fk_ai_run_conversation_id_conversation"
    )
