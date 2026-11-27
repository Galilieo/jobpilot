"""通过假模型上游捕获真实对话请求，固定上下文消息契约。"""

from json import loads
from typing import Any

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.main import app
from jobpilot.services.prompts import load_prompt
from jobpilot.tests.test_agent_api import (
    batch,
    conversation,
    done_of,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


async def test_next_turn_sends_prior_steps_and_only_current_attachment(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    requests: list[list[dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(loads(request.content)["messages"])
        if len(requests) == 1:
            return batch(("list_jobs", {}))
        return reply("可以继续")

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for content, attachment in [
            ("先看看岗位", "旧附件"),
            ("继续", "新附件"),
        ]:
            response = await async_client.post(
                f"{path}/messages",
                json=message_body(
                    content, attachments=[{"text": attachment, "name": "样例.txt"}]
                ),
            )
            assert done_of(response)["status"] == "succeeded"

    assert requests[-1] == [
        {
            "role": "system",
            "content": load_prompt("agent").text + "\n任务状态\n"
            '<untrusted_data label="task_state">\n'
            '{"binding_revision":0,"facts":null,"active_card":null,"job":null,'
            '"match":"未匹配","resume":null}\n</untrusted_data>',
        },
        {"role": "user", "content": "先看看岗位"},
        {
            "role": "assistant",
            "content": (
                '<untrusted_data label="past_step">\n'
                '[{"tool":"list_jobs","total":0,"tool_call_id":"call-0"}]\n'
                "</untrusted_data>"
            ),
        },
        {"role": "assistant", "content": "可以继续"},
        {
            "role": "user",
            "content": '继续\n<untrusted_data label="attachment_1">\n新附件\n'
            "</untrusted_data>",
        },
    ]
    # 界面看的细节存在步骤记录里，但上面回传模型的 past_step 没有它。
    saved = (await async_client.get(path)).json()["messages"]
    steps = [step for message in saved for step in message["steps_json"]]
    assert steps[0]["detail"] == {"jobs": []}
