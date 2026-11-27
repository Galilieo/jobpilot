"""以假上游和真实确认接口验证版本绑定、重放及撤销。"""

from json import loads
from uuid import UUID

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.main import app
from jobpilot.tests.test_agent_api import batch, card_of, done_of, message_body, reply
from jobpilot.tests.test_applications_api import presented_resume as presented_resume
from jobpilot.tests.test_call_accounting import fake_upstream


async def test_agent_card_binds_version_and_undo_requires_created_revision(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
) -> None:
    _, job_id, version = presented_resume
    conversation = await async_client.post(
        "/api/conversations", json={"current_job_id": job_id}
    )
    path = f"/api/conversations/{conversation.json()['conversation_id']}"
    async with fake_upstream(_record_application_reply) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first_card = await _application_card(async_client, path)
        assert first_card["args"]["resume_version_id"] == version["version_id"]
        current = await async_client.get("/api/tailored-resumes")
        run_id = current.json()[0]["run_id"]
        saved = await async_client.put(
            f"/api/tailor-runs/{run_id}/resume",
            json={
                "base_version_id": version["version_id"],
                "content": {**version["content"], "header": ["第二版"]},
            },
        )
        assert saved.is_success, saved.text
        application_id = await _confirm_application_card(
            async_client, path, first_card["action_id"]
        )
        repeated_id = await _confirm_application_card(
            async_client, path, first_card["action_id"]
        )
        assert repeated_id == application_id
        assert len((await async_client.get("/api/applications")).json()) == 1
        response = await async_client.get(f"/api/applications/{application_id}")
        assert response.json()["resume_version_id"] == version["version_id"]
        assert response.json()["snapshot_json"] == version["content"]
        assert (
            await async_client.post(f"/api/actions/{first_card['action_id']}/undo")
        ).is_success
        assert (
            await async_client.get(f"/api/applications/{application_id}")
        ).status_code == 404

        second_card = await _application_card(async_client, path)
        second_id = await _confirm_application_card(
            async_client, path, second_card["action_id"]
        )
        changed = await async_client.put(
            f"/api/applications/{second_id}",
            json={"stage": "written_test", "expected_revision": 1},
        )
        assert changed.is_success
        late_undo = await async_client.post(
            f"/api/actions/{second_card['action_id']}/undo"
        )
        assert late_undo.status_code == 409


def _record_application_reply(request: httpx.Request) -> httpx.Response:
    if loads(request.content)["messages"][-1]["role"] == "tool":
        return reply()
    return batch(("record_application", {"channel": "官网"}))


async def _application_card(client: httpx.AsyncClient, path: str) -> dict:
    sent = await client.post(f"{path}/messages", json=message_body("我投了"))
    return card_of(sent)


async def _confirm_application_card(
    client: httpx.AsyncClient, path: str, action_id: str
) -> str:
    confirmed = await client.post(
        f"{path}/actions/{action_id}", json={"decision": "confirm"}
    )
    assert confirmed.is_success, confirmed.text
    return done_of(confirmed)["action"]["result"]["application_id"]
