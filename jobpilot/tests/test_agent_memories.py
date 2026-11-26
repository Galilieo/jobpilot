"""通过真实对话、确认和撤销路由验证记忆，模型请求由假上游接收。"""

import asyncio
from json import loads

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    batch,
    card_of,
    conversation,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


def _tone(quote: str) -> httpx.Response:
    return batch(
        ("remember", {"key": "tone", "op": "set", "value": "平实", "quote": quote})
    )


async def _send(client: httpx.AsyncClient, path: str, text: str, **fields: object):
    return await client.post(f"{path}/messages", json=message_body(text, **fields))


async def _confirm(client: httpx.AsyncClient, path: str, action_id: str):
    return await client.post(
        f"{path}/actions/{action_id}", json={"decision": "confirm"}
    )


async def test_two_cards_cannot_both_create_the_same_memory(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    first, second = await conversation(async_client), await conversation(async_client)
    text = "以后语气朴实一点"

    def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content)["messages"][-1]["role"] == "tool":
            return reply()
        return _tone(text)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        cards = [
            card_of(await _send(async_client, path, text)) for path in (first, second)
        ]
        assert cards[0]["action_id"] != cards[1]["action_id"]
        results = await asyncio.gather(
            *(
                _confirm(async_client, path, card["action_id"])
                for path, card in zip((first, second), cards, strict=True)
            )
        )
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(index for index, result in enumerate(results) if result.is_success)
    undone = await async_client.post(f"/api/actions/{cards[winner]['action_id']}/undo")
    assert undone.is_success
    loser = 1 - winner
    stale = await _confirm(
        async_client, (first, second)[loser], cards[loser]["action_id"]
    )
    assert stale.status_code == 409
    records = (await async_client.get("/api/memories")).json()
    tone = next(row for row in records if row["key"] == "tone")
    assert {k: tone[k] for k in ("key", "value", "revision", "deleted")} == {
        "key": "tone",
        "value": None,
        "revision": 2,
        "deleted": True,
    }


@pytest.mark.parametrize(
    ("body", "quote", "attachment"),
    [
        ("可以", "可以", None),
        ("以后语气不用朴实一点", "朴实一点", None),
        ("请看附件", "以后语气朴实一点", "以后语气朴实一点"),
        ("以后语气朴实一点", "以后语气朴实一点", "以后语气朴实一点"),
    ],
)
async def test_memory_requires_an_unambiguous_current_body_quote(
    async_client: httpx.AsyncClient, body: str, quote: str, attachment: str | None
):
    await sign_in(async_client)
    path = await conversation(async_client)
    async with fake_upstream(lambda _: _tone(quote)) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        fields = {"attachments": [{"text": attachment}]} if attachment else {}
        sent = await _send(async_client, path, body, **fields)
    cards = [card for event, card in events(sent) if event == "confirm"]
    assert [card["tool"] for card in cards] == ["ask_user"]


async def test_memory_is_recalled_in_a_new_conversation_and_removed_after_forget(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    value = "仅考虑虚构甲类岗位"
    saved = await async_client.put(
        "/api/memories/job_notes", json={"expected_revision": 0, "value": value}
    )
    assert saved.status_code == 200
    path = await conversation(async_client)
    requests: list[list[dict[str, str]]] = []
    quote = "忘掉其他求职偏好"

    def respond(request: httpx.Request) -> httpx.Response:
        messages = loads(request.content)["messages"]
        requests.append(messages)
        if messages[-1]["role"] == "tool":
            return reply()
        return batch(("forget", {"key": "job_notes", "op": "clear", "quote": quote}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(await _send(async_client, path, quote))
        response = await _confirm(async_client, path, card["action_id"])
        assert response.status_code == 200

    assert value in requests[0][0]["content"]
    assert value not in str(requests[1])
    assert value not in str(events(response))
    assert value not in str((await async_client.get(path)).json())
    undone = await async_client.post(f"/api/actions/{card['action_id']}/undo")
    assert undone.is_success
    assert value not in undone.text
    preferences = (await async_client.get("/api/memories")).json()
    job_notes = next(item for item in preferences if item["key"] == "job_notes")
    assert {k: job_notes[k] for k in ("key", "value", "revision", "deleted")} == {
        "key": "job_notes",
        "value": value,
        "revision": 3,
        "deleted": False,
    }


async def test_list_add_and_replace_compute_the_card_value_on_the_server(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    created = await async_client.put(
        "/api/memories/cities", json={"expected_revision": 0, "value": ["杭州"]}
    )
    assert created.status_code == 200

    def respond(request: httpx.Request) -> httpx.Response:
        last = loads(request.content)["messages"][-1]
        if last["role"] == "tool":
            return reply()
        quote = last["content"]
        op, value = ("add", ["上海"]) if "也行" in quote else ("replace", ["苏州"])
        return batch(
            ("remember", {"key": "cities", "op": op, "value": value, "quote": quote})
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for text, value in [
            ("上海也行", ["杭州", "上海"]),
            ("目标城市改成苏州", ["苏州"]),
        ]:
            card = card_of(await _send(async_client, path, text))
            assert card["args"]["value"] == value
            assert (await _confirm(async_client, path, card["action_id"])).is_success


async def test_body_used_as_jd_cannot_supply_a_memory_quote(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    text = "岗位职责和要求：以后语气朴实一点"

    def respond(request: httpx.Request) -> httpx.Response:
        if not loads(request.content).get("stream"):
            return httpx.Response(400, json={"error": {"message": "模拟 JD 解析失败"}})
        return batch(
            ("remember", {"key": "tone", "op": "set", "value": "平实", "quote": text}),
            ("parse_jd", {"source": "message"}),
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        sent = await _send(async_client, path, text)
    assert card_of(sent)["tool"] == "ask_user"
