"""验证岗位页发起改写时保存的偏好，在后台执行前被修改也能重用。"""

from json import loads
from uuid import UUID

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.api.tailoring import RunLauncher
from jobpilot.db.models import AiRun
from jobpilot.main import app
from jobpilot.services.prompts import load_prompt
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_api import launcher as launcher
from jobpilot.tests.test_tailoring_api import sign_in, start
from jobpilot.tests.test_tailoring_graph import Factory, FakeModel, seed_inputs


async def test_tailoring_sends_preferences_from_run_creation(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await sign_in(async_client, "writing-snapshot@example.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    path = "/api/memories/tone"
    old = await async_client.put(path, json={"expected_revision": 0, "value": "平实"})
    assert old.status_code == 200
    model = FakeModel(inputs.fact_ids)
    requests: list[str] = []
    pending = []
    launch = launcher.launch
    monkeypatch.setattr(launcher, "launch", lambda *args: pending.append(args))

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        system = payload["messages"][0]["content"]
        requests.append(system)
        # 假上游按提示词类型选择输出，不参与措辞检查。
        model._kinds[system] = model._kinds[system.split("\n\n写作偏好\n", 1)[0]]
        return model(request)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        started = await start(async_client, inputs.user_job_id)
        assert started.status_code == 202
        try:
            new = await async_client.put(
                path, json={"expected_revision": 1, "value": "简洁有力"}
            )
            assert new.status_code == 200
        finally:
            launch(*pending.pop())
            await launcher.wait_all()

    rewrite = load_prompt("rewrite_bullets").text
    write_requests = [system for system in requests if system.startswith(rewrite)]
    assert write_requests and all("语气平实" in system for system in write_requests)
    assert load_prompt("match_job").text in requests
    assert load_prompt("ground_check").text in requests
    assert all("简洁有力" not in system for system in write_requests)
    async with session_factory() as reader:
        run = await reader.get_one(AiRun, UUID(started.json()["run_id"]))
    assert run.status == "succeeded"
    assert run.writing_preferences_json == {"tone": "平实"}
