"""通过对话与假模型验证自动记录、撤销和冻结快照。"""

import asyncio
from json import loads

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.db.memory_schema import MEMORY_AUTO_MAX_BODY_CHARS
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    batch,
    card_of,
    conversation,
    done_of,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream

QUOTE = "我不投外包"
CONTENT = "不投外包公司"


def remember(quote: str = QUOTE, **changes):
    return "remember", {
        "category": "job_pref",
        "content": CONTENT,
        "quote": quote,
        **changes,
    }


async def send(client: httpx.AsyncClient, path: str, text: str, **fields):
    return await client.post(f"{path}/messages", json=message_body(text, **fields))


async def call(client: httpx.AsyncClient, path: str, text: str, *calls, **fields):
    def respond(request: httpx.Request):
        messages = loads(request.content)["messages"]
        return reply() if messages[-1]["role"] == "tool" else batch(*calls)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await send(client, path, text, **fields)
    emitted = events(result)
    assert not any(event == "confirm" for event, _ in emitted)
    return [data for event, data in emitted if event == "tool_end"]


async def entries(client: httpx.AsyncClient) -> list[dict]:
    return (await client.get("/api/memories")).json()["entries"]


@pytest.mark.parametrize(
    ("body", "attachments"), [("你好", []), ("看附件", [{"text": QUOTE}])]
)
async def test_source_must_be_exact_current_body(
    async_client: httpx.AsyncClient, body: str, attachments: list
):
    await sign_in(async_client)
    path = await conversation(async_client)
    async with fake_upstream(lambda _: reply()) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await send(async_client, path, QUOTE)
    (step,) = await call(async_client, path, body, remember(), attachments=attachments)
    assert step["status"] == 422
    assert await entries(async_client) == []


async def test_body_used_as_jd_cannot_also_be_remembered(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = await conversation(async_client)
    steps = await call(
        async_client, path, QUOTE, remember(), ("parse_jd", {"source": "message"})
    )
    assert steps[0]["status"] == 422
    assert await entries(async_client) == []


async def test_record_duplicate_and_undo_add_replace_forget(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = await conversation(async_client)
    first, duplicate = await call(async_client, path, QUOTE, remember(), remember())
    assert first["detail"]["quote"] == QUOTE
    assert duplicate["status"] == "exists"
    (saved,) = await entries(async_client)
    memory_id = saved["memory_id"]
    assert saved["content"] == CONTENT
    assert saved["conversation_title"]
    assert saved["source"]["quote"] == QUOTE

    new_quote = "我优先投自研岗"
    (replacement,) = await call(
        async_client,
        path,
        new_quote,
        remember(new_quote, content=new_quote, replaces=memory_id[:8]),
    )
    assert replacement["memory_id"] == memory_id
    assert (await entries(async_client))[0]["revision"] == 2
    undo_replace = f"/api/actions/{replacement['detail']['action_id']}/undo"
    assert (await async_client.post(undo_replace)).json()["result"]["revision"] == 3
    assert (await async_client.post(undo_replace)).json()["result"]["revision"] == 3
    assert (await entries(async_client))[0]["content"] == CONTENT

    forget = ("forget", {"memory_id": memory_id, "quote": "忘掉外包偏好"})
    async with fake_upstream(
        lambda request: (
            reply()
            if loads(request.content)["messages"][-1]["role"] == "tool"
            else batch(forget)
        )
    ) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        forget_response = await send(async_client, path, "忘掉外包偏好")
        card = card_of(forget_response)
        assert card["tool"] == "forget"
        assert card["args"]["content"] == CONTENT
        assert card["args"]["quote"] == "忘掉外包偏好"
        assert len(await entries(async_client)) == 1
        confirmed = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
    assert done_of(confirmed)["status"] == "succeeded"
    assert await entries(async_client) == []
    assert (await async_client.post(f"/api/actions/{card['action_id']}/undo")).json()[
        "result"
    ]["revision"] == 5
    assert (await entries(async_client))[0]["content"] == CONTENT
    assert set((await async_client.get(path)).json()["undone_record_actions"]) == {
        replacement["detail"]["action_id"],
        card["action_id"],
    }

    (new,) = await call(async_client, path, QUOTE, remember(content="排除劳务派遣"))
    undo_add = await async_client.post(
        f"/api/actions/{new['detail']['action_id']}/undo"
    )
    assert undo_add.json()["result"]["revision"] == 2
    assert len(await entries(async_client)) == 1


@pytest.mark.parametrize(
    ("body", "quote"),
    [
        ("不要忘掉外包那条", "忘掉外包那条"),
        ("我接受外包，但仅限暑期岗。", "我接受外包"),
    ],
)
async def test_partial_sentence_quote_is_rejected(
    async_client: httpx.AsyncClient, body: str, quote: str
):
    await sign_in(async_client)
    path = await conversation(async_client)
    (step,) = await call(async_client, path, body, remember(quote))
    assert step["status"] == 422
    assert await entries(async_client) == []


async def test_suspicious_body_and_attachments_require_confirmation_card(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = await conversation(async_client)
    long_jd = "岗位职责：负责后端开发。" * 25 + "\n" + QUOTE
    assert len(long_jd) > MEMORY_AUTO_MAX_BODY_CHARS

    def respond(request: httpx.Request):
        messages = loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return reply()
        return batch(remember())

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = await send(async_client, path, long_jd)
        card = card_of(first)
        assert card["tool"] == "remember"
        assert card["args"]["content"] == CONTENT
        assert card["args"]["quote"] == QUOTE
        assert await entries(async_client) == []
        confirmed = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
        assert done_of(confirmed)["status"] == "succeeded"
        (saved,) = await entries(async_client)
        assert saved["content"] == CONTENT
        undo = await async_client.post(f"/api/actions/{card['action_id']}/undo")
        assert undo.status_code == 200
        assert await entries(async_client) == []

        with_attachment = await send(
            async_client, path, QUOTE, attachments=[{"text": "岗位附件文本"}]
        )
        att_card = card_of(with_attachment)
        assert att_card["tool"] == "remember"
        rejected = await async_client.post(
            f"{path}/actions/{att_card['action_id']}", json={"decision": "reject"}
        )
        assert done_of(rejected)["status"] == "succeeded"
        assert await entries(async_client) == []


async def test_external_tool_in_same_turn_forces_confirmation_card(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    other_path = await conversation(async_client)
    path = await conversation(async_client)
    other_id = other_path.rsplit("/", 1)[-1]

    def respond(request: httpx.Request):
        messages = loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            if len([m for m in messages if m["role"] == "tool"]) == 1:
                return batch(remember())
            return reply()
        return batch(
            (
                "read_conversation_history",
                {"conversation_id": other_id, "start_seq": 1},
            )
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await send(async_client, other_path, "旧对话里的内容")
        res = await send(async_client, path, QUOTE)
        card = card_of(res)
        assert card["tool"] == "remember"
        assert await entries(async_client) == []
        confirmed = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
    assert done_of(confirmed)["status"] == "succeeded"
    assert len(await entries(async_client)) == 1


async def test_undo_races_page_write_and_writing_preference_can_undo(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = await conversation(async_client)
    quote = "以后简历写得朴实一些"
    tool = (
        "set_writing_preference",
        {"field": "tone", "value": "平实", "quote": quote},
    )
    (set_step,) = await call(async_client, path, quote, tool)
    assert set_step["status"] == "preference_set"
    undo_path = f"/api/actions/{set_step['detail']['action_id']}/undo"
    assert (await async_client.post(undo_path)).json()["result"]["revision"] == 2
    assert (await async_client.get("/api/writing-preference")).json()["values"][
        "tone"
    ] is None
    (first,) = await call(async_client, path, QUOTE, remember())
    (row,) = await entries(async_client)
    results = await asyncio.gather(
        async_client.put(
            f"/api/memories/{row['memory_id']}",
            json={
                "expected_revision": 1,
                "content": "排除兼职",
                "category": "job_pref",
            },
        ),
        async_client.post(f"/api/actions/{first['detail']['action_id']}/undo"),
    )
    assert sorted(response.status_code for response in results) == [200, 409]


async def test_record_budget_includes_entries_and_limits_successful_writes(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = await conversation(async_client)
    steps = await call(
        async_client,
        path,
        QUOTE,
        *(remember(content=char) for char in ("甲", "乙", "丙", "丁")),
    )
    assert [step["status"] for step in steps] == [
        "remembered",
        "remembered",
        "remembered",
        422,
    ]
    for index in range(7):
        assert (
            await async_client.post(
                "/api/memories",
                json={"category": "job_pref", "content": str(index) * 200},
            )
        ).is_success
    records = []

    def respond(request: httpx.Request):
        messages = loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            records.append(
                loads(messages[-1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
            )
            return reply()
        return batch(remember(content="甲" * 150))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await send(async_client, path, QUOTE)
    assert done_of(result)["status"] == "succeeded"
    assert len(records[0]["entries"]) == 10
    assert records[0]["status"] == 409


async def test_snapshot_rebuilds_next_turn_when_memories_change(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    conv_a = await conversation(async_client)
    conv_b = await conversation(async_client)
    system_messages = []

    def respond(request: httpx.Request):
        payload = loads(request.content)
        if payload.get("stream"):
            system_messages.append(payload["messages"][0]["content"])
            if payload["messages"][-1]["content"] == QUOTE:
                return batch(remember())
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        res_a = await send(async_client, conv_a, QUOTE)
        assert done_of(res_a)["status"] == "succeeded"
        action_id = next(
            data["detail"]["action_id"]
            for name, data in events(res_a)
            if name == "tool_end" and data["tool"] == "remember"
        )
        assert (
            done_of(await send(async_client, conv_b, "B 第一轮"))["status"]
            == "succeeded"
        )
        assert CONTENT in system_messages[-1]
        prefix_before = system_messages[-1]
        assert (
            done_of(await send(async_client, conv_b, "B 第二轮"))["status"]
            == "succeeded"
        )
        assert system_messages[-1] == prefix_before

        assert (
            await async_client.post(f"/api/actions/{action_id}/undo")
        ).status_code == 200
        assert (
            done_of(await send(async_client, conv_a, "A 下一轮"))["status"]
            == "succeeded"
        )
        assert CONTENT not in system_messages[-1]
        assert (
            done_of(await send(async_client, conv_b, "B 下一轮"))["status"]
            == "succeeded"
        )
        assert CONTENT not in system_messages[-1]
        conv_c = await conversation(async_client)
        assert (
            done_of(await send(async_client, conv_c, "C 新开"))["status"] == "succeeded"
        )
        assert CONTENT not in system_messages[-1]


async def test_record_memory_succeeds_after_confirmation_card_resume(
    async_client: httpx.AsyncClient,
    session_factory,
):
    from datetime import UTC, datetime
    from uuid import UUID

    from jobpilot.db.models import FactSet, ResumeDocument
    from jobpilot.tests.test_tailored_resumes_api import _seed_tailored_resume

    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    async with session_factory.begin() as db:
        doc = ResumeDocument(user_id=user_id, raw_text="合成简历")
        fact_set = FactSet(user_id=user_id, version=1)
        db.add_all([doc, fact_set])
        await db.flush()
        job_id, _ = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=fact_set.fact_set_id,
            job_title="后端实习",
            job_company="示例公司",
            version_number=1,
            created_at=datetime.now(UTC),
        )
    conv = await async_client.post(
        "/api/conversations", json={"current_job_id": str(job_id)}
    )
    path = f"/api/conversations/{conv.json()['conversation_id']}"
    body = "我刚投了这个岗位。我不投外包"

    def respond(request: httpx.Request):
        messages = loads(request.content)["messages"]
        last = messages[-1]
        if last["role"] == "tool":
            tools_seen = [m for m in messages if m["role"] == "tool"]
            if len(tools_seen) == 1:
                return batch(remember())
            return reply("已记录投递并记住偏好")
        return batch(("record_application", {}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = await send(async_client, path, body)
        card = card_of(first)
        assert card["tool"] == "record_application"
        resumed = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
    assert done_of(resumed)["status"] == "succeeded"
    (saved,) = await entries(async_client)
    assert saved["content"] == CONTENT
    assert len((await async_client.get("/api/applications")).json()) == 1


async def test_record_memory_rejects_when_confirmation_card_still_pending(
    async_client: httpx.AsyncClient,
    session_factory,
):
    from uuid import UUID

    from jobpilot.db.conversations import owned_agent_run
    from jobpilot.services.agent.memory_actions import RememberArgs, record_memory
    from jobpilot.services.agent.types import AgentDeps, ToolCall, ToolContext

    await sign_in(async_client)
    path = await conversation(async_client)
    conv_id = UUID(path.rsplit("/", 1)[-1])
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])

    async with fake_upstream(
        lambda _: batch(("ask_user", {"questions": ["哪家公司？"]}))
    ) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = await send(async_client, path, QUOTE)
        card = card_of(first)

    async with session_factory() as db:
        run = await owned_agent_run(user_id, db, conv_id, UUID(card["run_id"]))
        assert run is not None
        deps = AgentDeps(
            session_factory,
            fake,
            user_id,
            conv_id,
            run.run_id,
            lambda *_: None,
            0.0,
            60.0,
        )
        ctx = ToolContext(
            deps,
            ToolCall(call_id="c1", name="remember", arguments="{}", key="1:0:c1"),
        )
        res = await record_memory(
            ctx, RememberArgs(category="job_pref", content=CONTENT, quote=QUOTE)
        )
    assert res["status"] == 409
    assert await entries(async_client) == []
