"""通过 Agent 消息接口验证跨对话工具和独立的 Ollama HTTP 边界。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.db.conversations import add_message
from jobpilot.db.models import (
    AiRun,
    Conversation,
    MessageChunk,
    MessageRole,
    RunStatus,
    User,
)
from jobpilot.main import app
from jobpilot.services.accounts import purge_accounts
from jobpilot.services.message_index import backfill_message_index
from jobpilot.tests.test_agent_api import (
    batch,
    conversation,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


@pytest.fixture
def ollama_inputs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    inputs: list[str] = []

    async def embed(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.url.path == "/api/embed"
        texts = json.loads(request.content)["input"]
        inputs.extend(texts)
        return httpx.Response(
            200, json={"embeddings": [[1, *([0] * 1023)]] * len(texts)}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", embed)
    return inputs


async def test_search_finds_a_saved_message_and_returns_a_source_link(
    async_client: httpx.AsyncClient, ollama_inputs: list[str]
) -> None:
    """等待旧轮次的后台索引，再从新对话的正常工具协议检索。"""
    await sign_in(async_client)
    old_path = await conversation(async_client)
    new_path = await conversation(async_client)
    body = "云岬科技的实习每周要求到岗四天"
    results: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        last = json.loads(request.content)["messages"][-1]
        if last["role"] == "tool":
            content = last["content"]
            results.append(content)
            if len(results) == 1:
                result = json.loads(content.splitlines()[1])
                if result.get("hits"):
                    match = result["hits"][0]
                    return batch(
                        (
                            "read_conversation_history",
                            {
                                "conversation_id": match["conversation_id"],
                                "start_seq": match["seq"],
                                "end_seq": match["seq"],
                            },
                        )
                    )
        elif last["content"] == "查一下前次到岗安排":
            return batch(("search_conversations", {"query": "到岗"}))
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        assert (
            await async_client.post(f"{old_path}/messages", json=message_body(body))
        ).is_success
        await app.state.run_launcher.wait_all()
        response = await async_client.post(
            f"{new_path}/messages", json=message_body("查一下前次到岗安排")
        )
        await app.state.run_launcher.wait_all()

    [step] = [
        payload
        for name, payload in events(response)
        if name == "tool_end" and payload["tool"] == "search_conversations"
    ]
    assert step.get("error") is None
    [hit] = step["detail"]["hits"]
    assert hit["snippet"] == body
    assert hit["conversation_id"] == old_path.rsplit("/", 1)[-1]
    assert hit["seq"] > 0
    assert body in ollama_inputs
    assert len(results) == 2
    assert all(
        result.startswith('<untrusted_data label="tool_result">') for result in results
    )
    history = json.loads(results[1].splitlines()[1])
    assert [row["text"] for row in history["rows"]] == [body]


@pytest.mark.parametrize(
    "query", ["云岬科技", "上次聊过的实习到岗安排，你还有印象吗？！"]
)
async def test_offline_ollama_keeps_messages_and_returns_keyword_hits(
    async_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    query: str,
) -> None:
    await sign_in(async_client)
    old_path = await conversation(async_client)
    new_path = await conversation(async_client)
    body = "云岬科技 实习每周到岗四天"

    def respond(request: httpx.Request) -> httpx.Response:
        last = json.loads(request.content)["messages"][-1]
        if last["content"] == "请查上一段聊过的公司":
            return batch(("search_conversations", {"query": query}))
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        assert (
            await async_client.post(f"{old_path}/messages", json=message_body(body))
        ).is_success
        await app.state.run_launcher.wait_all()
        response = await async_client.post(
            f"{new_path}/messages", json=message_body("请查上一段聊过的公司")
        )

    [step] = [payload for name, payload in events(response) if name == "tool_end"]
    assert step["detail"]["degraded"]
    [hit] = step["detail"]["hits"]
    assert hit["snippet"] == body
    assert hit["source"] == "keyword"
    assert any(
        row["content"] == body
        for row in (await async_client.get(old_path)).json()["messages"]
    )
    metrics = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith('{"event": "ollama_embedding"')
    ]
    assert metrics and all(item["duration_ms"] >= 0 for item in metrics)
    assert body not in caplog.text


async def test_backfill_keeps_exact_spans_and_ignores_non_message_content(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_inputs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧消息走补算入口，检查真实向量列和原文偏移。"""
    await sign_in(async_client)
    path = await conversation(async_client)
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    body = "第一段说明。\n\n" + "包含原文🙂记号。" * 59 + "最后一句。" + "尾" * 520
    async with session_factory.begin() as session:
        saved = await session.get_one(Conversation, conversation_id)
        run = AiRun(
            user_id=saved.user_id,
            conversation_id=conversation_id,
            kind="agent",
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        await add_message(
            session,
            run,
            MessageRole.USER,
            "input",
            body,
            attachments=[{"text": "不应索引的附件原文"}],
        )
        await add_message(
            session, run, MessageRole.TOOL, "tool:1", "工具正文不能进索引"
        )
        await add_message(
            session, run, MessageRole.ASSISTANT, "draft", "临时草稿不能进索引"
        )
        await add_message(session, run, MessageRole.ASSISTANT, "final", "好的")

    first = await backfill_message_index(session_factory)
    embedded = tuple(ollama_inputs)
    second = await backfill_message_index(session_factory)
    after_second = tuple(ollama_inputs)
    monkeypatch.setattr(settings, "embedding_model", "bge-m3:updated")
    third = await backfill_message_index(session_factory)

    async with session_factory() as session:
        chunks = list(
            await session.scalars(select(MessageChunk).order_by(MessageChunk.chunk_no))
        )
    assert first.chunks_created == first.embeddings_written == len(chunks) >= 4
    assert (second.chunks_created, second.embeddings_written) == (0, 0)
    assert after_second == embedded
    assert third.chunks_created == 0
    assert third.embeddings_written == len(chunks)
    assert [chunk.content for chunk in chunks] == [
        body[chunk.span_start : chunk.span_end] for chunk in chunks
    ]
    assert "".join(chunk.content for chunk in chunks) == body
    assert chunks[0].content == "第一段说明。\n\n"
    assert chunks[1].content.endswith("最后一句。")
    assert all(len(chunk.content) <= 500 for chunk in chunks)
    assert all(chunk.embedding is not None and chunk.search_terms for chunk in chunks)
    assert {chunk.embedding_model for chunk in chunks} == {"bge-m3:updated"}
    assert set(embedded) == {chunk.content for chunk in chunks}


async def test_chunks_cascade_on_conversation_and_account_delete(
    async_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in(async_client)
    deleted, retained = (
        await conversation(async_client),
        await conversation(async_client),
    )
    async with fake_upstream(lambda _: reply()) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for path in (deleted, retained):
            assert (
                await async_client.post(
                    f"{path}/messages", json=message_body("中文测试原文")
                )
            ).is_success
        await app.state.run_launcher.wait_all()

    async def count_chunks() -> int:
        async with session_factory() as session:
            return await session.scalar(select(func.count()).select_from(MessageChunk))

    assert await count_chunks() == 2
    assert "message_chunk" not in (await async_client.get("/api/account/export")).json()
    assert (await async_client.delete(deleted)).status_code == 204
    assert await count_chunks() == 1
    assert (await async_client.delete("/api/account")).status_code == 204
    async with session_factory.begin() as session:
        await session.execute(
            update(User).values(
                deletion_requested_at=datetime.now(UTC) - timedelta(days=8)
            )
        )
    assert (await purge_accounts(session_factory)).deleted == 1
    assert await count_chunks() == 0
