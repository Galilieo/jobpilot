"""通过本地 HTTP 套接字在首个工具事件后断开，验证后台继续提交。"""

import asyncio
import socket
from contextlib import asynccontextmanager
from json import dumps, loads

import httpx
import uvicorn

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.main import app
from jobpilot.tests.test_agent_api import batch, conversation, reply, sign_in
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream
from jobpilot.tests.test_jobs_api import JD, OUTPUT


@asynccontextmanager
async def _listening_http():
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield listener.getsockname()[1]
        finally:
            server.should_exit = True
            await task


async def _disconnect_at_tool(
    port: int,
    path: str,
    token: str,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        body = dumps({"content": JD}).encode()
        request = (
            f"POST {path}/messages HTTP/1.1\r\nHost: localhost\r\n"
            f"Cookie: {settings.cookie_name}={token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        writer.write(request.encode() + body)
        await writer.drain()
        prefix = await asyncio.wait_for(
            reader.readuntil(b"event: tool_start\n"), timeout=5
        )
        assert prefix.startswith(b"HTTP/1.1 200")
    finally:
        writer.close()
        await writer.wait_closed()


async def test_job_parse_finishes_after_http_client_disconnects(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    parsed = asyncio.Event()
    send_count = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal send_count
        payload = loads(request.content)
        if payload["messages"][-1]["role"] == "tool":
            return reply("岗位已加入岗位库")
        if payload.get("stream"):
            return batch(("parse_jd", {"raw_text": JD}))
        send_count += 1
        await parsed.wait()
        return completion_response(dumps(OUTPUT))

    async with fake_upstream(respond) as fake, _listening_http() as port:
        app.dependency_overrides[get_model_client] = lambda: fake
        try:
            await _disconnect_at_tool(
                port, path, async_client.cookies[settings.cookie_name]
            )
        finally:
            parsed.set()
        await asyncio.wait_for(app.state.run_launcher.wait_all(), timeout=5)

    details = (await async_client.get(path)).json()
    jobs = (await async_client.get("/api/jobs")).json()
    assert details["run"]["status"] == "succeeded"
    assert details["messages"][-1]["content"] == "岗位已加入岗位库"
    assert send_count == len(jobs) == 1
    assert details["current_job_id"] == jobs[0]["job_id"]
