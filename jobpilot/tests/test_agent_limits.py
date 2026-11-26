"""使用假模型验证每轮工具限制和交给上游的输入大小。"""

from json import loads
from uuid import UUID, uuid4

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.db.models import JobPosting, UserJob
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    batch,
    conversation,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_graph import (
    SNAPSHOT,
    Factory,
    FakeModel,
    seed_inputs,
)


async def test_identical_generation_reuses_result_and_distinct_calls_have_limit(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, owner)
    job_ids = [inputs.user_job_id]
    async with session_factory.begin() as db:
        for index in range(2):
            posting = JobPosting(
                owner_id=owner,
                raw_text=f"岗位 {index}",
                content_hash=uuid4().hex,
                parsed_json=SNAPSHOT,
            )
            db.add(posting)
            await db.flush()
            job = UserJob(user_id=owner, posting_id=posting.posting_id)
            db.add(job)
            await db.flush()
            job_ids.append(job.user_job_id)
    path = await conversation(async_client)
    assert (
        await async_client.put(path, json={"current_job_id": str(job_ids[0])})
    ).is_success
    downstream = FakeModel(inputs.fact_ids)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if not payload.get("stream"):
            return downstream(request)
        if payload["messages"][-1]["role"] == "tool":
            return reply()
        return batch(
            ("match_job", {}),
            *(("match_job", {"job_id": str(job_id)}) for job_id in job_ids),
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        response = await async_client.post(
            f"{path}/messages", json=message_body("匹配岗位")
        )
    first, duplicate, second, blocked = [
        payload for name, payload in events(response) if name == "tool_end"
    ]
    assert duplicate["report_id"] == first["report_id"]
    assert second["report_id"] != first["report_id"]
    assert blocked["status"] == 422 and "上限" in blocked["error"]
    assert downstream.calls["match"] == 2


async def test_combined_input_limit_rejects_before_starting(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        response = await async_client.post(
            f"{path}/messages",
            json=message_body("字" * 10000, attachments=[{"text": "文" * 20000}]),
        )
    assert response.status_code == 422
    assert "总长度" in response.json()["detail"][0]["msg"]
    assert requests == []
    assert (await async_client.get(path)).json()["run"] is None


async def test_large_tool_result_remains_small_and_valid_json(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, owner)
    async with session_factory.begin() as db:
        job = await db.get_one(UserJob, inputs.user_job_id)
        posting = await db.get_one(JobPosting, job.posting_id)
        posting.raw_text = "一段岗位说明" * 3000
    path = await conversation(async_client)
    received: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        last_message = loads(request.content)["messages"][-1]
        if last_message["role"] == "tool":
            received.append(last_message["content"])
            return reply()
        return batch(("read_job", {"job_id": str(inputs.user_job_id)}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        await async_client.post(f"{path}/messages", json=message_body("读岗位"))
    [wrapped] = received
    content = wrapped.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert len(content.encode()) < 8000
    result = loads(content)
    assert result["job_id"] == str(inputs.user_job_id) and result["truncated"] is True
