"""用户隔离测试框架：用户 A 不能读、改、删用户 B 的资源，且每个接口都必须归类。

新增接口时在下面三处之一登记，否则 test_every_api_route_is_classified 失败：
- PUBLIC_ROUTES：不需要登录（注册、登录、健康检查）。
- SELF_ROUTES：只作用于调用者自己，路径里没有资源 ID（登出、当前用户）。
- OWNED_RESOURCES：属于某个用户的资源。登记创建方式、B 读取自己资源的接口，
  以及 A 会尝试的每个请求；A 的每次尝试都应返回 404，且 B 的资源保持不变。
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from jobpilot.api.deps import get_session_factory
from jobpilot.db.models import FactSet, ResumeDocument
from jobpilot.main import app
from jobpilot.tests.test_auth import login, register
from jobpilot.tests.test_facts_api import new_idempotency_headers
from jobpilot.tests.test_tailored_resumes_api import _seed_tailored_resume

Route = tuple[str, str]  # (HTTP 方法, 路径模板)

PUBLIC_ROUTES: set[Route] = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
}
SELF_ROUTES: set[Route] = {
    ("POST", "/api/auth/logout"),
    ("GET", "/api/me"),
    ("GET", "/api/overview"),
    ("DELETE", "/api/account"),
    ("POST", "/api/account/restore"),
    ("GET", "/api/account/export"),
    ("GET", "/api/memories"),
    ("PUT", "/api/memories/{key}"),
    ("DELETE", "/api/memories/{key}"),
    ("POST", "/api/extract-text"),  # 只提取文字，不读写任何已保存的数据
    ("POST", "/api/ocr"),  # 识别结果直接返回，不读写任何已保存的数据
}


@dataclass(frozen=True)
class Attempt:
    """A 对 B 的资源发起的一次请求。路径和请求体中的 "{id}" 会替换成 B 的资源 ID。

    请求体里放 "{id}" 用来测嵌套引用，例如 A 自己的会话引用 B 的岗位。
    """

    method: str
    path: str
    json: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class OwnedResource:
    """一类属于用户的资源及其全部接口。"""

    name: str
    # 以 B 的身份创建，返回 ID
    create: Callable[[httpx.AsyncClient, list[str]], Awaitable[str]]
    read: Route  # B 读取自己这条资源的接口，用来比较前后是否被改
    attempts: tuple[Attempt, ...]
    # 该资源的全部接口（含创建、列表），用于归类检查。
    routes: frozenset[Route] = field(default_factory=frozenset)


async def _create_fact(client: httpx.AsyncClient, _model_outputs: list[str]) -> str:
    """B 补充一条事实并发布，A 要碰的是已发布的事实。"""
    created = await client.post(
        "/api/facts",
        json={
            "experience": "B 的项目",
            "kind": "action",
            "responsibility": "own",
            "text": "B 写的事实",
        },
    )
    assert created.status_code == 201, created.text
    committed = await client.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": None,
            "expected_revision": created.json()["revision"],
        },
    )
    assert committed.status_code == 200, committed.text
    return created.json()["lineage_id"]


async def _create_education(
    client: httpx.AsyncClient, _model_outputs: list[str]
) -> str:
    """B 补充一条教育经历并发布，返回 education_id。"""
    created = await client.post(
        "/api/facts/education",
        json={
            "school": "B 的学校",
            "major": "计算机",
            "degree": "bachelor",
            "status": "enrolled",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 201, created.text
    committed = await client.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": None,
            "expected_revision": created.json()["revision"],
        },
    )
    assert committed.status_code == 200, committed.text
    return created.json()["education_id"]


async def _create_resume(client: httpx.AsyncClient, _model_outputs: list[str]) -> str:
    """B 上传一份简历；假模型返回「没有经历」，只留下原文。"""
    created = await client.post(
        "/api/resumes",
        json={"raw_text": "B 的简历原文"},
        headers=new_idempotency_headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["doc_id"]


async def _create_job(client: httpx.AsyncClient, _model_outputs: list[str]) -> str:
    """B 提交一份 JD；假模型返回空的解析结果。"""
    created = await client.post(
        "/api/jobs",
        json={"raw_text": "B 收藏的岗位"},
        headers=new_idempotency_headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["job_id"]


async def _create_match_report(
    client: httpx.AsyncClient, model_outputs: list[str]
) -> str:
    """B 发布一条事实、建一个带要求的岗位并生成匹配报告。"""
    await _create_fact(client, model_outputs)
    fact_id = (await client.get("/api/facts")).json()["facts"][0]["fact_id"]
    created_job = await client.post(
        "/api/jobs",
        json={"raw_text": "B 的匹配岗位"},
        headers=new_idempotency_headers(),
    )
    assert created_job.status_code == 201, created_job.text
    job_id = created_job.json()["job_id"]
    updated = await client.put(
        f"/api/jobs/{job_id}",
        json={"requirements": [{"type": "hard", "text": "熟悉 Python"}]},
    )
    assert updated.status_code == 200, updated.text
    req_id = updated.json()["parsed"]["requirements"][0]["requirement_id"]
    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": req_id,
                        "verdict": "covered",
                        "fact_ids": [fact_id],
                        "reason": "已掌握",
                        "suggestion": "保留",
                    }
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    matched = await client.post(
        f"/api/jobs/{job_id}/match", headers=new_idempotency_headers()
    )
    assert matched.status_code == 201, matched.text
    return matched.json()["report_id"]


async def _create_tailor_run(
    client: httpx.AsyncClient, model_outputs: list[str]
) -> str:
    """B 上传简历、生成匹配报告，再对同一岗位启动改写，等后台任务结束。"""
    await _create_resume(client, model_outputs)
    report_id = await _create_match_report(client, model_outputs)
    job_id = (await client.get(f"/api/match-reports/{report_id}")).json()["job_id"]
    started = await client.post(
        f"/api/jobs/{job_id}/tailor",
        json={"sections": {"B 的项目": "project"}},
        headers=new_idempotency_headers(),
    )
    assert started.status_code == 202, started.text
    await app.state.run_launcher.wait_all()
    return started.json()["run_id"]


async def _application_source(client: httpx.AsyncClient) -> tuple[str, str]:
    user_id = UUID((await client.get("/api/me")).json()["user_id"])
    factory = app.dependency_overrides[get_session_factory]()
    async with factory.begin() as db:
        doc = ResumeDocument(user_id=user_id, raw_text="合成简历")
        fact_set = FactSet(user_id=user_id, version=1)
        db.add_all([doc, fact_set])
        await db.flush()
        job_id, run_id = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=fact_set.fact_set_id,
            job_title="示例岗位",
            job_company="示例公司",
            version_number=1,
            created_at=datetime.now(UTC),
        )
    resume = await client.get(f"/api/tailor-runs/{run_id}/resume")
    assert resume.status_code == 200, resume.text
    return str(job_id), resume.json()["version_id"]


async def _create_application(
    client: httpx.AsyncClient, model_outputs: list[str]
) -> str:
    job_id, version_id = await _application_source(client)
    response = await client.post(
        "/api/applications",
        json={"job_id": job_id, "resume_version_id": version_id},
        headers=new_idempotency_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()["application_id"]


# T1.4 起每加一类业务资源，就在这里登记一项。
async def _create_conversation(client: httpx.AsyncClient, _outputs: list[str]) -> str:
    response = await client.post("/api/conversations", json={"title": "B 的对话"})
    assert response.status_code == 201, response.text
    return response.json()["conversation_id"]


OWNED_RESOURCES: list[OwnedResource] = [
    OwnedResource(
        name="conversation",
        create=_create_conversation,
        read=("GET", "/api/conversations/{id}"),
        attempts=(
            Attempt("GET", "/api/conversations/{id}"),
            Attempt("PUT", "/api/conversations/{id}", {"title": "A 的修改"}),
            Attempt("DELETE", "/api/conversations/{id}"),
            Attempt("POST", "/api/conversations/{id}/compact"),
            Attempt(
                "POST",
                "/api/conversations/{id}/messages",
                {
                    "content": "A 的消息",
                    "client_message_id": "cc8f3e6c-6b1b-46f4-83fe-c272878628e1",
                },
            ),
            Attempt(
                "POST", "/api/conversations/{id}/actions/{id}", {"decision": "confirm"}
            ),
        ),
        routes=frozenset(
            {
                ("GET", "/api/conversations"),
                ("POST", "/api/conversations"),
                ("GET", "/api/conversations/{conversation_id}"),
                ("PUT", "/api/conversations/{conversation_id}"),
                ("DELETE", "/api/conversations/{conversation_id}"),
                ("POST", "/api/conversations/{conversation_id}/messages"),
                ("POST", "/api/conversations/{conversation_id}/compact"),
                ("POST", "/api/conversations/{conversation_id}/actions/{action_id}"),
                ("POST", "/api/actions/{action_id}/undo"),
            }
        ),
    ),
    OwnedResource(
        name="fact",
        create=_create_fact,
        read=("GET", "/api/facts/{id}"),
        attempts=(
            Attempt("GET", "/api/facts/{id}"),
            Attempt(
                "PUT",
                "/api/facts/{id}",
                {"text": "A 改的", "confirmed": True, "expected_revision": 0},
            ),
            Attempt("DELETE", "/api/facts/{id}?expected_revision=0"),
        ),
        routes=frozenset(
            {
                ("GET", "/api/facts"),
                ("POST", "/api/facts"),
                ("GET", "/api/facts/{lineage_id}"),
                ("PUT", "/api/facts/{lineage_id}"),
                ("DELETE", "/api/facts/{lineage_id}"),
                ("POST", "/api/facts/batch"),
                ("POST", "/api/facts/commit"),
                ("PUT", "/api/facts/criteria"),
            }
        ),
    ),
    OwnedResource(
        name="education",
        create=_create_education,
        read=("GET", "/api/facts"),
        attempts=(
            Attempt(
                "PUT",
                "/api/facts/education/{id}",
                {"school": "A 改的", "confirmed": True, "expected_revision": 0},
            ),
            Attempt("DELETE", "/api/facts/education/{id}?expected_revision=0"),
        ),
        routes=frozenset(
            {
                ("POST", "/api/facts/education"),
                ("PUT", "/api/facts/education/{education_id}"),
                ("DELETE", "/api/facts/education/{education_id}"),
            }
        ),
    ),
    OwnedResource(
        name="resume_document",
        create=_create_resume,
        read=("GET", "/api/resumes/{id}"),
        attempts=(Attempt("GET", "/api/resumes/{id}"),),
        routes=frozenset(
            {
                ("POST", "/api/resumes"),
                ("GET", "/api/resumes/{doc_id}"),
            }
        ),
    ),
    OwnedResource(
        name="job",
        create=_create_job,
        read=("GET", "/api/jobs/{id}"),
        attempts=(
            Attempt("GET", "/api/jobs/{id}"),
            Attempt("GET", "/api/applications?job_id={id}"),
            Attempt("POST", "/api/conversations", {"current_job_id": "{id}"}),
            Attempt("PUT", "/api/jobs/{id}", {"archived": True, "note": "A 写的"}),
            Attempt("DELETE", "/api/jobs/{id}"),
            Attempt("POST", "/api/jobs/{id}/match", headers=new_idempotency_headers()),
            Attempt("GET", "/api/jobs/{id}/matches"),
            Attempt("GET", "/api/jobs/{id}/tailor-runs"),
            Attempt(
                "POST", "/api/jobs/{id}/tailor", {}, headers=new_idempotency_headers()
            ),
        ),
        routes=frozenset(
            {
                ("POST", "/api/jobs"),
                ("GET", "/api/jobs"),
                ("GET", "/api/jobs/{job_id}"),
                ("PUT", "/api/jobs/{job_id}"),
                ("DELETE", "/api/jobs/{job_id}"),
                ("POST", "/api/jobs/{job_id}/match"),
                ("GET", "/api/jobs/{job_id}/matches"),
                ("GET", "/api/jobs/{job_id}/tailor-runs"),
                ("POST", "/api/jobs/{job_id}/tailor"),
            }
        ),
    ),
    OwnedResource(
        name="application",
        create=_create_application,
        read=("GET", "/api/applications/{id}"),
        attempts=(
            Attempt("GET", "/api/applications/{id}"),
            Attempt("GET", "/api/applications/{id}/snapshot.html"),
            Attempt("PUT", "/api/applications/{id}", {"expected_revision": 1}),
            Attempt("DELETE", "/api/applications/{id}", {"expected_revision": 1}),
        ),
        routes=frozenset(
            {
                ("GET", "/api/applications"),
                ("POST", "/api/applications"),
                ("GET", "/api/applications/{application_id}"),
                ("PUT", "/api/applications/{application_id}"),
                ("DELETE", "/api/applications/{application_id}"),
                ("GET", "/api/applications/{application_id}/snapshot.html"),
            }
        ),
    ),
    OwnedResource(
        name="match_report",
        create=_create_match_report,
        read=("GET", "/api/match-reports/{id}"),
        attempts=(Attempt("GET", "/api/match-reports/{id}"),),
        routes=frozenset({("GET", "/api/match-reports/{report_id}")}),
    ),
    OwnedResource(
        name="tailor_run",
        create=_create_tailor_run,
        read=("GET", "/api/tailor-runs/{id}"),
        attempts=(
            Attempt("GET", "/api/tailor-runs/{id}"),
            Attempt("GET", "/api/tailor-runs/{id}/resume"),
            Attempt("GET", "/api/tailor-runs/{id}/preview"),
            Attempt("GET", "/api/tailor-runs/{id}/export"),
            Attempt(
                "PUT",
                "/api/tailor-runs/{id}/resume",
                {
                    "base_version_id": "{id}",
                    "content": {"header": [], "sections": []},
                },
            ),
            Attempt(
                "POST",
                "/api/tailor-runs/{id}/resume/reset",
                {"base_version_id": "{id}"},
            ),
            Attempt(
                "POST",
                "/api/tailor-runs/{id}/resume/bullets/{id}/use-facts",
                {"base_version_id": "{id}"},
            ),
        ),
        routes=frozenset(
            {
                ("GET", "/api/tailored-resumes"),
                ("GET", "/api/tailor-runs/{run_id}"),
                ("GET", "/api/tailor-runs/{run_id}/resume"),
                ("PUT", "/api/tailor-runs/{run_id}/resume"),
                ("POST", "/api/tailor-runs/{run_id}/resume/reset"),
                (
                    "POST",
                    "/api/tailor-runs/{run_id}/resume/bullets/{bullet_id}/use-facts",
                ),
                ("GET", "/api/tailor-runs/{run_id}/preview"),
                ("GET", "/api/tailor-runs/{run_id}/export"),
            }
        ),
    ),
]


def _fill(value: Any, resource_id: str) -> Any:
    """把路径或请求体中的 "{id}" 占位替换成真实资源 ID。"""
    if isinstance(value, str):
        return value.replace("{id}", resource_id)
    if isinstance(value, dict):
        return {key: _fill(item, resource_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, resource_id) for item in value]
    return value


def _api_routes() -> set[Route]:
    """从 OpenAPI 文档列出全部 /api 接口。

    FastAPI 0.163 起 include_router 不再把子路由展开到 app.routes，遍历它会漏掉
    挂载的接口，所以改用公开的 OpenAPI 文档。代价：include_in_schema=False 的接口
    不会被列出，JobPilot 不使用这个选项。
    """
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api")
        for method in operations
    }


async def _sign_in(client: httpx.AsyncClient, email: str) -> None:
    credentials = {"email": email, "password": "isolation-test-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success


@pytest.fixture
async def user_a(async_client: httpx.AsyncClient) -> httpx.AsyncClient:
    await _sign_in(async_client, "a@isolation.invalid")
    return async_client


@pytest.fixture
async def user_b(
    async_client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    """另一个客户端，有自己的 Cookie。依赖 async_client，保证接口已指向测试库。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _sign_in(client, "b@isolation.invalid")
        yield client


# ---------- 归类：新接口忘了登记时在这里失败 ----------


def test_every_api_route_is_classified() -> None:
    # 先确认能列出已知接口，防止列表为空时下面的检查空过。
    assert PUBLIC_ROUTES | SELF_ROUTES <= _api_routes()
    owned = {route for resource in OWNED_RESOURCES for route in resource.routes}
    unclassified = _api_routes() - PUBLIC_ROUTES - SELF_ROUTES - owned

    assert not unclassified, (
        f"这些接口还没登记到隔离测试：{sorted(unclassified)}，"
        "请加入 PUBLIC_ROUTES、SELF_ROUTES 或 OWNED_RESOURCES"
    )


def test_self_routes_take_no_resource_id() -> None:
    """SELF 接口只能从登录态知道「是谁」。路径一旦带 {id}，就可能访问别人的资源，
    必须改登记到 OWNED_RESOURCES，接受 404 隔离测试。"""
    with_id = sorted(
        route
        for route in SELF_ROUTES
        if "{" in route[1] and route[1] != "/api/memories/{key}"
    )

    assert not with_id, f"这些 SELF 接口带路径参数：{with_id}"


@pytest.mark.parametrize(
    "route", sorted(_api_routes() - PUBLIC_ROUTES), ids=lambda r: f"{r[0]} {r[1]}"
)
async def test_protected_routes_require_login(
    async_client: httpx.AsyncClient, route: Route
) -> None:
    """未登录访问任何非公开接口都返回 401。"""
    method, path = route
    response = await async_client.request(
        method, _fill(path, str(uuid4())).replace("{key}", "tone"), json={}
    )

    assert response.status_code == 401


# ---------- 隔离：A 访问 B 的资源 ----------


async def test_self_routes_only_show_the_caller(
    user_a: httpx.AsyncClient, user_b: httpx.AsyncClient
) -> None:
    assert (await user_a.get("/api/me")).json()["email"] == "a@isolation.invalid"
    assert (await user_b.get("/api/me")).json()["email"] == "b@isolation.invalid"


@pytest.mark.parametrize("resource", OWNED_RESOURCES, ids=lambda r: r.name)
async def test_other_users_resources_are_not_found(
    user_a: httpx.AsyncClient,
    user_b: httpx.AsyncClient,
    resource: OwnedResource,
    model_outputs: list[str],
) -> None:
    """A 的每次尝试都返回 404（不是 403，不暴露资源是否存在），B 的资源不变。"""
    resource_id = await resource.create(user_b, model_outputs)
    read_method, read_path = resource.read
    before = await user_b.request(read_method, _fill(read_path, resource_id))
    assert before.status_code == 200

    for attempt in resource.attempts:
        response = await user_a.request(
            attempt.method,
            _fill(attempt.path, resource_id),
            json=_fill(attempt.json, resource_id),
            headers=attempt.headers,
        )
        assert response.status_code == 404, (attempt, response.text)

    after = await user_b.request(read_method, _fill(read_path, resource_id))
    assert after.json() == before.json()


PENDING_ACCOUNT_ROUTES: dict[Route, int] = {
    ("GET", "/api/me"): 200,
    ("GET", "/api/account/export"): 200,
    ("POST", "/api/account/restore"): 204,
    ("POST", "/api/auth/logout"): 204,
}


@pytest.mark.parametrize(
    "route", sorted(_api_routes() - PUBLIC_ROUTES), ids=lambda r: f"{r[0]} {r[1]}"
)
async def test_routes_for_pending_account(
    async_client: httpx.AsyncClient, route: Route
) -> None:
    """待删除账号重新登录后，只能查状态、取回数据、恢复或登出。"""
    email = "pending@isolation.invalid"
    assert (await register(async_client, email)).status_code == 201
    assert (await login(async_client, email)).status_code == 204
    assert (await async_client.delete("/api/account")).status_code == 204
    assert (await login(async_client, email)).status_code == 204

    method, path = route
    response = await async_client.request(
        method,
        _fill(path, str(uuid4())).replace("{key}", "tone"),
        json={},
        headers=new_idempotency_headers(),
    )

    assert response.status_code == PENDING_ACCOUNT_ROUTES.get(route, 403), response.text


async def test_application_cannot_reference_another_users_resume_version(
    user_a: httpx.AsyncClient,
    user_b: httpx.AsyncClient,
    model_outputs: list[str],
) -> None:
    _, other_version = await _application_source(user_b)
    own_job = await _create_job(user_a, model_outputs)
    response = await user_a.post(
        "/api/applications",
        json={"job_id": own_job, "resume_version_id": other_version},
        headers=new_idempotency_headers(),
    )
    assert response.status_code == 404, response.text
    assert (await user_a.get("/api/applications")).json() == []
