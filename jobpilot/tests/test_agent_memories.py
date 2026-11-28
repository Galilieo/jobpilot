"""通过对话与假模型验证自动记录、撤销和冻结快照。"""

import asyncio
from json import loads

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
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
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream

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


async def test_snapshot_is_frozen_until_compaction_and_new_conversation(
    async_client: httpx.AsyncClient, monkeypatch
):
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "context_window_cap_tokens", 24_000)
    system_messages = []

    def respond(request: httpx.Request):
        payload = loads(request.content)
        if payload.get("stream"):
            system_messages.append(payload["messages"][0]["content"])
            return reply()
        return completion_response('{"goal":"准备改写"}')

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        assert (
            done_of(await send(async_client, path, "第一轮"))["status"] == "succeeded"
        )
        await async_client.post(
            "/api/memories", json={"category": "agent_rule", "content": CONTENT}
        )
        await async_client.put(
            "/api/writing-preference", json={"expected_revision": 0, "tone": "平实"}
        )
        assert (
            done_of(await send(async_client, path, "第二轮"))["status"] == "succeeded"
        )
        assert CONTENT not in system_messages[-1]
        assert 'label="long_term_memory"' not in system_messages[-1]
        fresh = await conversation(async_client)
        assert (
            done_of(await send(async_client, fresh, "新对话"))["status"] == "succeeded"
        )
        assert CONTENT in system_messages[-1]
        assert '"tone":"平实"' in system_messages[-1]
        for _ in range(3):
            assert (
                done_of(await send(async_client, path, "乙" * 2300))["status"]
                == "succeeded"
            )
            await app.state.run_launcher.wait_all()
        assert (
            done_of(await send(async_client, path, "压缩后"))["status"] == "succeeded"
        )
        assert 'label="work_summary"' in system_messages[-1]
        assert CONTENT in system_messages[-1]
