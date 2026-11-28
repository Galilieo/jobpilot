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


async def test_app_notices_are_not_indexed_but_model_answers_are(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_inputs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """程序生成的固定提示没有检索价值，额度用完后模型给的答复仍要能搜到。"""
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "tool_calls", 1)

    def respond(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "user" and messages[-1]["content"] == "这轮会出错":
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        if messages[-1]["role"] == "tool":
            return reply("按已经查到的岗位回答")
        return batch(("list_jobs", {}), ("list_jobs", {}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for text in ("这轮会出错", "列两次岗位"):
            sent = await async_client.post(f"{path}/messages", json=message_body(text))
            assert sent.is_success
            await app.state.run_launcher.wait_all()

    shown = [m["content"] for m in (await async_client.get(path)).json()["messages"]]
    assert "本轮对话未完成。可以查看已完成步骤，或重新发消息。" in shown
    async with session_factory() as session:
        indexed = set(await session.scalars(select(MessageChunk.content)))
    assert indexed == {"这轮会出错", "列两次岗位", "按已经查到的岗位回答"}


async def test_identical_texts_take_one_search_position(
    async_client: httpx.AsyncClient, ollama_inputs: list[str]
) -> None:
    """按钮发出的同一句话出现在多段对话里，只占一个位置，其余留给别的结果。"""
    await sign_in(async_client)
    repeated = "从岗位库里选一个岗位继续准备。"
    wanted = "木槿协作的技术栏写了 Temporal"
    paths = [await conversation(async_client) for _ in range(5)]

    def respond(request: httpx.Request) -> httpx.Response:
        last = json.loads(request.content)["messages"][-1]
        if last["content"] == "找 Temporal":
            return batch(("search_conversations", {"query": "Temporal 岗位"}))
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await async_client.post(f"{paths[0]}/messages", json=message_body(wanted))
        for path in paths[1:4]:
            await async_client.post(f"{path}/messages", json=message_body(repeated))
            await app.state.run_launcher.wait_all()
        response = await async_client.post(
            f"{paths[4]}/messages", json=message_body("找 Temporal")
        )
        await app.state.run_launcher.wait_all()

    [step] = [payload for name, payload in events(response) if name == "tool_end"]
    snippets = [hit["snippet"] for hit in step["detail"]["hits"]]
    assert snippets.count(repeated) == 1
    assert wanted in snippets


async def test_search_redacts_multiline_phone_before_serialization(
    async_client: httpx.AsyncClient, ollama_inputs: list[str]
) -> None:
    await sign_in(async_client)
    old_path = await conversation(async_client)
    new_path = await conversation(async_client)
    body = "云岬科技联系方式\n138\n1234\n5678"
    captured: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            captured.append(messages[-1]["content"])
            return reply()
        if messages[-1].get("content") == "查联系方式":
            return batch(("search_conversations", {"query": "云岬科技"}))
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        assert (
            await async_client.post(f"{old_path}/messages", json=message_body(body))
        ).is_success
        await app.state.run_launcher.wait_all()
        assert (
            await async_client.post(
                f"{new_path}/messages", json=message_body("查联系方式")
            )
        ).is_success

    assert len(captured) == 1
    assert "[PHONE]" in captured[0]
    assert "138" not in captured[0]
    assert "5678" not in captured[0]


async def test_vector_results_precede_pending_keyword_backfill_and_dedupe(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_inputs: list[str],
) -> None:
    from jobpilot.db.conversations import find_conversation
    from jobpilot.services.conversation_terms import search_terms

    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    old_paths = [await conversation(async_client) for _ in range(7)]
    new_path = await conversation(async_client)
    embedded_only = "精确命中的向量历史"
    duplicate_text = "项目周报重复内容"

    async with session_factory.begin() as session:
        model_name = settings.embedding_model
        specs = [
            (old_paths[0], embedded_only, [1.0, *([0.0] * 1023)], model_name),
            (old_paths[1], duplicate_text, [0.8, 0.6, *([0.0] * 1022)], model_name),
            (old_paths[2], duplicate_text, None, None),
            *((old_paths[i], f"项目待算消息 {i}", None, None) for i in range(3, 7)),
        ]
        for path, text_body, vector, chunk_model in specs:
            cid = UUID(path.rsplit("/", 1)[-1])
            conv = await find_conversation(user_id, session, cid)
            assert conv is not None
            run = AiRun(
                user_id=user_id,
                conversation_id=cid,
                kind="agent",
                status=RunStatus.SUCCEEDED,
                finished_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            await add_message(session, run, MessageRole.USER, "input", text_body)
            await session.flush()
            from jobpilot.db.models import Message

            msg = await session.scalar(
                select(Message).where(Message.run_id == run.run_id)
            )
            assert msg is not None
            session.add(
                MessageChunk(
                    message_id=msg.message_id,
                    conversation_id=cid,
                    user_id=user_id,
                    chunk_no=0,
                    span_start=0,
                    span_end=len(text_body),
                    content=text_body,
                    embedding=vector,
                    embedding_model=chunk_model,
                    search_terms=func.to_tsvector("simple", search_terms(text_body)),
                )
            )

    tool_outputs: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            content = messages[-1]["content"]
            tool_outputs.append(json.loads(content.splitlines()[1]))
            return reply()
        return batch(("search_conversations", {"query": "项目", "limit": 5}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await async_client.post(f"{new_path}/messages", json=message_body("查项目"))

    payload = tool_outputs[0]
    assert payload["degraded"] is False
    assert payload["index_pending"] is True
    snippets = [hit["snippet"] for hit in payload["hits"]]
    assert len(snippets) == 5
    assert embedded_only in snippets
    assert snippets.count(duplicate_text) == 1
    assert snippets[:2] == [embedded_only, duplicate_text]


async def test_search_steps_store_locators_only_and_follow_source_deletion(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_inputs: list[str],
) -> None:
    from jobpilot.db.models import Message

    await sign_in(async_client)
    source_path = await conversation(async_client)
    target_path = await conversation(async_client)
    secret_text = "木槿协作面试问到了 Temporal 工作流"

    def respond(request: httpx.Request) -> httpx.Response:
        last = json.loads(request.content)["messages"][-1]
        if last.get("content") == "查一下木槿协作":
            return batch(("search_conversations", {"query": "Temporal"}))
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await async_client.post(
            f"{source_path}/messages", json=message_body(secret_text)
        )
        await app.state.run_launcher.wait_all()
        await async_client.post(
            f"{target_path}/messages", json=message_body("查一下木槿协作")
        )
        await app.state.run_launcher.wait_all()

    target_id = UUID(target_path.rsplit("/", 1)[-1])
    async with session_factory() as session:
        tool_msgs = list(
            await session.scalars(
                select(Message).where(
                    Message.conversation_id == target_id,
                    Message.role == MessageRole.TOOL,
                )
            )
        )
    assert len(tool_msgs) == 1
    stored_hit = tool_msgs[0].steps_json[0]["detail"]["hits"][0]
    assert "snippet" not in stored_hit
    assert "title" not in stored_hit
    assert stored_hit["conversation_id"] == source_path.rsplit("/", 1)[-1]
    assert stored_hit["seq"] > 0

    before_detail = (await async_client.get(target_path)).json()
    before_hit = next(
        step["detail"]["hits"][0]
        for msg in before_detail["messages"]
        for step in msg["steps_json"]
        if step.get("tool") == "search_conversations"
    )
    assert before_hit["snippet"] == secret_text
    assert before_hit["source_deleted"] is False

    export_before = (await async_client.get("/api/account/export")).json()
    exported_target_steps = [
        step
        for msg in export_before["messages"]
        if msg["conversation_id"] == str(target_id)
        for step in msg["steps_json"]
        if step.get("tool") == "search_conversations"
    ]
    assert "snippet" not in exported_target_steps[0]["detail"]["hits"][0]

    assert (await async_client.delete(source_path)).status_code == 204

    after_detail = (await async_client.get(target_path)).json()
    after_hit = next(
        step["detail"]["hits"][0]
        for msg in after_detail["messages"]
        for step in msg["steps_json"]
        if step.get("tool") == "search_conversations"
    )
    assert after_hit["source_deleted"] is True
    assert after_hit.get("snippet") is None

    export_after = (await async_client.get("/api/account/export")).json()
    assert secret_text not in json.dumps(export_after, ensure_ascii=False)


async def test_legacy_stored_search_snippet_is_ignored_on_read_and_export(
    async_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await sign_in(async_client)
    target_path = await conversation(async_client)
    target_id = UUID(target_path.rsplit("/", 1)[-1])
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    legacy_secret = "已经删掉的旧摘录副本"

    async with session_factory.begin() as session:
        run = AiRun(
            user_id=user_id,
            conversation_id=target_id,
            kind="agent",
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        await add_message(
            session,
            run,
            MessageRole.TOOL,
            "tool:1:0:c1",
            "",
            steps=[
                {
                    "tool": "search_conversations",
                    "total": 1,
                    "detail": {
                        "query": "旧词",
                        "degraded": False,
                        "hits": [
                            {
                                "conversation_id": str(UUID(int=999)),
                                "seq": 1,
                                "title": "已删旧标题",
                                "snippet": legacy_secret,
                            }
                        ],
                    },
                }
            ],
        )

    detail = (await async_client.get(target_path)).json()
    hit = detail["messages"][0]["steps_json"][0]["detail"]["hits"][0]
    assert hit["source_deleted"] is True
    assert hit.get("snippet") is None
    assert hit.get("title") is None

    exported = (await async_client.get("/api/account/export")).json()
    assert legacy_secret not in json.dumps(exported, ensure_ascii=False)
