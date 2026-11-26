"""用真实数据库、接口和假模型验证压缩边界、去重及来源校验。"""

from asyncio import Event, timeout
from json import dumps, loads
from uuid import UUID

import httpx
from sqlalchemy import select

from jobpilot.api.deps import get_model_client
from jobpilot.db.conversations import conversation_messages
from jobpilot.db.models import Conversation, ModelCall
from jobpilot.main import app
from jobpilot.tests.test_agent_api import conversation, message_body, reply, sign_in
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream
from jobpilot.tests.test_tailoring_graph import Factory


async def test_compaction_is_deduplicated_and_does_not_cover_later_messages(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    arrived, release = Event(), Event()
    compact_calls = []
    main_messages = []
    first_seq = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if payload.get("stream"):
            main_messages.append(payload["messages"])
            return reply()
        compact_calls.append(payload)
        arrived.set()
        await release.wait()
        return completion_response(
            dumps(
                {
                    "goal": "跟踪岗位",
                    "requirements": [
                        {
                            "id": "r1",
                            "statement": "不考虑外包",
                            "quote": "不考虑外包",
                            "source": {"message_seq": first_seq},
                        },
                        {
                            "id": "r2",
                            "statement": "错误地引用附件",
                            "quote": "只在附件中",
                            "source": {"message_seq": first_seq},
                        },
                    ],
                },
                ensure_ascii=False,
            )
        )

    async with fake_upstream(respond) as fake, timeout(8):
        app.dependency_overrides[get_model_client] = lambda: fake
        for body in [
            message_body("不考虑外包", attachments=[{"text": "只在附件中"}]),
            message_body("第二轮原文"),
        ]:
            response = await async_client.post(f"{path}/messages", json=body)
            assert response.is_success
        user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
        async with session_factory() as db:
            before = await conversation_messages(user_id, db, conversation_id)
        first_seq = before[0].seq
        started = await async_client.post(f"{path}/compact", json={"focus": "岗位要求"})
        assert started.status_code == 202
        await arrived.wait()
        try:
            repeated = await async_client.post(
                f"{path}/compact", json={"focus": "改用别的重点"}
            )
            assert repeated.json()["run_id"] == started.json()["run_id"]
            assert repeated.json()["focus_applied"] is False
            late = await async_client.post(
                f"{path}/messages", json=message_body("压缩期间的新消息")
            )
            assert late.is_success
        finally:
            release.set()
            await app.state.run_launcher.wait_all()
        response = await async_client.post(
            f"{path}/messages", json=message_body("压缩后的新一轮")
        )
        assert response.is_success

    async with session_factory() as db:
        conversation_row = await db.get_one(Conversation, conversation_id)
        records = list(
            await db.scalars(select(ModelCall).where(ModelCall.purpose == "compact"))
        )
    assert 0 < conversation_row.summary_through_seq <= before[-1].seq
    assert conversation_row.summary_revision == 1
    assert [r["id"] for r in conversation_row.summary_json["requirements"]] == ["r1"]
    assert "错误地引用附件" in conversation_row.summary_json["unverified"]
    assert len(compact_calls) == len(records) == 1
    assert records[0].budget_run_id == UUID(started.json()["run_id"])
    assert "岗位要求" in str(compact_calls[0]["messages"])
    assert "改用别的重点" not in str(compact_calls[0]["messages"])
    assert (
        sum(item.get("content") == "压缩期间的新消息" for item in main_messages[-1])
        == 1
    )


async def test_manual_compaction_reports_no_work_and_conflicts_with_active_turn(
    async_client: httpx.AsyncClient,
) -> None:
    from jobpilot.tests.test_agent_api import batch, card_of

    await sign_in(async_client)
    path = await conversation(async_client)
    async with fake_upstream(
        lambda _: batch(("ask_user", {"questions": ["何时入职？"]}))
    ) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        empty = await async_client.post(f"{path}/compact")
        assert empty.status_code == 200
        assert empty.json() == {
            "run_id": None,
            "status": "not_needed",
            "focus_applied": False,
        }
        result = await async_client.post(f"{path}/messages", json=message_body("开始"))
        assert card_of(result)["tool"] == "ask_user"
        conflict = await async_client.post(f"{path}/compact")
    assert conflict.status_code == 409


async def test_auto_compaction_runs_after_threshold_and_reduces_next_request(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch,
) -> None:
    from jobpilot.config import settings
    from jobpilot.tests.test_agent_api import done_of

    await sign_in(async_client)
    path = await conversation(async_client)
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    monkeypatch.setattr(settings.agent_limits, "context_window_cap_tokens", 24_000)
    requests = []
    compact_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal compact_calls
        payload = loads(request.content)
        if payload.get("stream"):
            requests.append(payload["messages"])
            return reply("已记录")
        compact_calls += 1
        return completion_response('{"goal":"阶段目标"}')

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for index in range(3):
            result = await async_client.post(
                f"{path}/messages", json=message_body(f"第 {index} 轮 " + "甲" * 2300)
            )
            assert done_of(result)["status"] == "succeeded"
        await app.state.run_launcher.wait_all()
        assert compact_calls > 0
        result = await async_client.post(
            f"{path}/messages", json=message_body("新一轮")
        )
        assert done_of(result)["status"] == "succeeded"

    async with session_factory() as db:
        saved = await db.get_one(Conversation, conversation_id)
    assert saved.summary_revision >= 1
    assert saved.summary_json["goal"] == "阶段目标"
    assert 'label="work_summary"' in requests[-1][0]["content"]
    assert requests[-1][-1]["content"] == "新一轮"
    assert len(dumps(requests[-1], ensure_ascii=False).encode()) < len(
        dumps(requests[-2], ensure_ascii=False).encode()
    )


async def test_running_compaction_does_not_write_after_conversation_deletion(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    from jobpilot.db.models import AiRun

    await sign_in(async_client)
    path = await conversation(async_client)
    sent, release = Event(), Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content).get("stream"):
            return reply()
        sent.set()
        await release.wait()
        return completion_response('{"goal":"迟到摘要"}')

    async with fake_upstream(respond) as fake, timeout(8):
        app.dependency_overrides[get_model_client] = lambda: fake
        for text in ("第一轮", "第二轮"):
            await async_client.post(f"{path}/messages", json=message_body(text))
        started = await async_client.post(f"{path}/compact")
        assert started.status_code == 202
        run_id = UUID(started.json()["run_id"])
        await sent.wait()
        try:
            deleted = await async_client.delete(path)
            assert deleted.status_code == 204
        finally:
            release.set()
            await app.state.run_launcher.wait_all()
    async with session_factory() as db:
        assert await db.get(AiRun, run_id) is None
        record = await db.scalar(
            select(ModelCall).where(ModelCall.purpose == "compact")
        )
        assert (
            record is not None and record.run_id is None and record.outcome == "success"
        )


async def test_summary_accepts_a_submitted_question_as_source(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    from jobpilot.tests.test_agent_api import batch, card_of

    await sign_in(async_client)
    path = await conversation(async_client)
    action_id = ""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        last = payload["messages"][-1]
        if payload.get("stream"):
            if last.get("content") == "问我岗位条件":
                return batch(("ask_user", {"questions": ["接受外包吗？"]}))
            return reply()
        return completion_response(
            dumps(
                {
                    "requirements": [
                        {
                            "id": "outsource",
                            "statement": "排除外包",
                            "quote": "不接受外包",
                            "source": {"action_id": action_id, "question_id": "q1"},
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        sent = await async_client.post(
            f"{path}/messages", json=message_body("问我岗位条件")
        )
        action_id = card_of(sent)["action_id"]
        confirmed = await async_client.post(
            f"{path}/actions/{action_id}",
            json={"decision": "confirm", "answers": {"q1": "不接受外包"}},
        )
        assert confirmed.is_success
        await async_client.post(f"{path}/messages", json=message_body("继续"))
        assert (await async_client.post(f"{path}/compact")).status_code == 202
        await app.state.run_launcher.wait_all()

    async with session_factory() as db:
        saved = await db.get_one(Conversation, UUID(path.rsplit("/", 1)[-1]))
    assert saved.summary_json["requirements"][0]["id"] == "outsource"
    answer = saved.summary_json["answered_questions"][0]
    assert (answer["question"], answer["answer"]) == ("接受外包吗？", "不接受外包")


async def test_overflowed_compact_batch_is_smaller_on_later_manual_attempt(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    attempts = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if payload.get("stream"):
            return reply()
        attempts.append(payload["messages"])
        if len(attempts) == 1:
            return httpx.Response(
                400, json={"error": {"message": "Maximum context length exceeded"}}
            )
        return completion_response('{"goal":"保存旧任务信息"}')

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for index in range(4):
            result = await async_client.post(
                f"{path}/messages", json=message_body(f"第 {index} 轮" + "甲" * 900)
            )
            assert result.is_success
        for _ in range(2):
            assert (await async_client.post(f"{path}/compact")).status_code == 202
            await app.state.run_launcher.wait_all()

    assert len(attempts) == 2
    assert len(dumps(attempts[1], ensure_ascii=False).encode()) * 2 <= len(
        dumps(attempts[0], ensure_ascii=False).encode()
    )
    async with session_factory() as db:
        saved = await db.get_one(Conversation, UUID(path.rsplit("/", 1)[-1]))
        calls = await db.scalars(
            select(ModelCall)
            .where(ModelCall.purpose == "compact")
            .order_by(ModelCall.created_at)
        )
        assert [call.outcome for call in calls] == ["http_error", "success"]
    assert saved.summary_revision == 1


async def test_compaction_retries_are_separately_charged(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch,
) -> None:
    from jobpilot.config import settings

    await sign_in(async_client)
    path = await conversation(async_client)
    attempts = 0
    monkeypatch.setattr(settings, "model_retry_delay_seconds", 0)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if loads(request.content).get("stream"):
            return reply()
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {"message": "synthetic retry"}})
        return completion_response('{"goal":"重试摘要"}')

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for text in ("第一轮", "第二轮"):
            await async_client.post(f"{path}/messages", json=message_body(text))
        scheduled = await async_client.post(f"{path}/compact")
        assert scheduled.status_code == 202
        await app.state.run_launcher.wait_all()

    run_id = UUID(scheduled.json()["run_id"])
    async with session_factory() as db:
        ledger = list(
            await db.scalars(select(ModelCall).where(ModelCall.budget_run_id == run_id))
        )
    assert attempts == len(ledger) == 2
    assert {call.outcome for call in ledger} == {"http_error", "success"}
    assert all(call.run_id == run_id for call in ledger)


async def test_account_deletion_cancels_compact_without_writing_late_summary(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch,
) -> None:
    from jobpilot.db.models import AiRun

    await sign_in(async_client)
    path = await conversation(async_client)
    arrived, release = Event(), Event()
    launcher = app.state.run_launcher
    # 模拟已发出的请求由另一个进程持有，删号只能靠数据库阻止迟到写回。
    monkeypatch.setattr(launcher, "cancel_runs", lambda _run_ids: None)

    async def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content).get("stream"):
            return reply()
        arrived.set()
        await release.wait()
        return completion_response('{"goal":"迟到的摘要"}')

    async with fake_upstream(respond) as fake, timeout(8):
        app.dependency_overrides[get_model_client] = lambda: fake
        for text in ("第一轮", "第二轮"):
            await async_client.post(f"{path}/messages", json=message_body(text))
        started = await async_client.post(f"{path}/compact")
        run_id = UUID(started.json()["run_id"])
        await arrived.wait()
        try:
            assert (await async_client.delete("/api/account")).status_code == 204
        finally:
            release.set()
            await launcher.wait_all()

    async with session_factory() as db:
        saved = await db.get_one(Conversation, UUID(path.rsplit("/", 1)[-1]))
        run = await db.get_one(AiRun, run_id)
        record = await db.scalar(select(ModelCall).where(ModelCall.run_id == run_id))
    assert saved.summary_revision == 0
    assert run.status == "cancelled"
    assert record is not None and record.outcome == "success"


async def test_three_failures_wait_for_a_new_turn_before_auto_retry(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch,
) -> None:
    from jobpilot.config import settings
    from jobpilot.services.agent.compaction import CompactSchedule, schedule_compaction

    await sign_in(async_client)
    path = await conversation(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    monkeypatch.setattr(settings, "model_max_attempts", 1)
    monkeypatch.setattr(settings.agent_limits, "context_window_cap_tokens", 60_000)
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if loads(request.content).get("stream"):
            return reply()
        attempts += 1
        if attempts <= 3:
            return httpx.Response(503, json={"error": {"message": "临时故障"}})
        return completion_response('{"goal":"新一轮后恢复"}')

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        launcher = app.state.run_launcher
        for index in range(3):
            result = await async_client.post(
                f"{path}/messages", json=message_body(f"第 {index} 轮" + "甲" * 1800)
            )
            assert result.is_success
        for _ in range(3):
            assert (await async_client.post(f"{path}/compact")).status_code == 202
            await launcher.wait_all()
        assert attempts == 3

        monkeypatch.setattr(settings.agent_limits, "context_window_cap_tokens", 24_000)
        stopped = await schedule_compaction(
            CompactSchedule(
                session_factory,
                fake,
                launcher.launch,
                user_id,
                conversation_id,
                automatic=True,
            )
        )
        assert stopped.status == "not_needed"
        assert attempts == 3

        result = await async_client.post(
            f"{path}/messages", json=message_body("新增一轮" + "甲" * 1800)
        )
        assert result.is_success
        await launcher.wait_all()
    assert attempts == 4


async def test_rolling_summary_keeps_exception_and_its_withdrawal_in_order(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    job_id = "01111111-1111-4111-8111-111111111111"
    statements = [
        ("default", "不考虑外包", None, None),
        ("exception", "这个岗位可以例外", job_id, "default"),
        ("withdrawal", "撤回这个岗位的例外", job_id, "exception"),
    ]
    summaries = []
    sources = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if payload.get("stream"):
            return reply()
        index = len(summaries)
        summaries.append(payload["messages"][1]["content"])
        key, quote, scope, previous = statements[index]
        return completion_response(
            dumps(
                {
                    "requirements": [
                        {
                            "id": key,
                            "statement": quote,
                            "quote": quote,
                            "source": {"message_seq": sources[quote]},
                            "job_id": scope,
                            "supersedes": previous,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for index, (_, quote, _, _) in enumerate(statements):
            response = await async_client.post(
                f"{path}/messages", json=message_body(quote)
            )
            assert response.is_success
            async with session_factory() as db:
                messages = await conversation_messages(user_id, db, conversation_id)
            sources[quote] = next(row.seq for row in messages if row.content == quote)
            await async_client.post(
                f"{path}/messages", json=message_body(f"结束语 {index}")
            )
            assert (await async_client.post(f"{path}/compact")).status_code == 202
            await app.state.run_launcher.wait_all()

    async with session_factory() as db:
        saved = await db.get_one(Conversation, conversation_id)
    assert saved.summary_revision == 3
    assert [item["id"] for item in saved.summary_json["requirements"]] == [
        "default",
        "exception",
        "withdrawal",
    ]
    assert saved.summary_json["requirements"][-1]["supersedes"] == "exception"
    assert '"id":"default"' in summaries[1] and '"id":"exception"' in summaries[2]
    assert '"source":"user_body","text":"不考虑外包"' not in summaries[2]
