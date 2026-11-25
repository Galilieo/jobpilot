"""改写接口：启动时固定底稿与输入、后台执行、查询结果、跨用户 404、段落类型待指定、
程序错误和服务重启后的终态。

数据直接写进数据库，只经由改写接口和登录接口调用，不依赖上传、确认等其他接口。
"""

import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api import tailoring
from jobpilot.api.deps import get_model_client
from jobpilot.api.tailoring import RunLauncher, get_run_launcher
from jobpilot.db.models import (
    AiRun,
    CheckStatus,
    FactSet,
    ResumeDocument,
    RunStatus,
    SectionKind,
)
from jobpilot.db.runs import fail_abandoned_runs
from jobpilot.main import app
from jobpilot.services.grounding import GroundingIssue, IssueKind
from jobpilot.services.resume_content import (
    Entry,
    ResumeContent,
    ResumeSection,
    bullet_of,
)
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_fact_schema import add_user
from jobpilot.tests.test_facts_api import new_idempotency_headers
from jobpilot.tests.test_resume_export import embedded
from jobpilot.tests.test_tailored_resumes_api import _seed_tailored_resume
from jobpilot.tests.test_tailoring_graph import (
    INTERN,
    PROJECT,
    RAW_TEXT,
    FakeModel,
    seed_inputs,
)

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def launcher(async_client: httpx.AsyncClient) -> AsyncIterator[RunLauncher]:
    """替换后台任务，测试可以等改写跑完再查询。"""
    runner = RunLauncher()
    app.dependency_overrides[get_run_launcher] = lambda: runner
    yield runner
    await runner.wait_all()


async def sign_in(client: httpx.AsyncClient, email: str) -> UUID:
    credentials = {"email": email, "password": "tailor-api-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success
    return UUID((await client.get("/api/me")).json()["user_id"])


@pytest.fixture
async def model(async_client: httpx.AsyncClient) -> AsyncIterator[FakeModel]:
    fake = FakeModel(fact_ids=[])  # 测试写入数据后再填
    async with fake_upstream(fake) as client:
        app.dependency_overrides[get_model_client] = lambda: client
        yield fake


async def start(
    client: httpx.AsyncClient, job_id: UUID, *, key: str | None = None, **body
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key else new_idempotency_headers()
    return await client.post(f"/api/jobs/{job_id}/tailor", json=body, headers=headers)


async def add_document(session_factory: Factory, user_id: UUID) -> UUID:
    async with session_factory.begin() as session:
        doc = ResumeDocument(user_id=user_id, raw_text=RAW_TEXT)
        session.add(doc)
        await session.flush()
        return doc.doc_id


# ---------- 启动与查询 ----------


async def test_start_runs_in_background_and_result_reads_back(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "run@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids

    started = await start(async_client, inputs.user_job_id)
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    await launcher.wait_all()

    result = (await async_client.get(f"/api/tailor-runs/{run_id}")).json()
    assert (result["status"], result["stop_reason"]) == ("succeeded", None)
    runs = (
        await async_client.get(f"/api/jobs/{inputs.user_job_id}/tailor-runs")
    ).json()
    assert [(r["run_id"], r["status"], r["has_resume"]) for r in runs] == [
        (run_id, "succeeded", True)
    ]
    assert result["base_doc_id"] == str(inputs.doc_id)
    assert result["job_id"] == str(inputs.user_job_id)
    assert [b["status"] for b in result["blocks"]] == ["done", "done", "done"]
    assert [(b["experience"], b["check_status"]) for b in result["bullets"]] == [
        (INTERN, "passed"),
        (PROJECT, "passed"),
        (None, "passed"),
    ]
    assert len(result["bullets"][0]["fact_ids"]) == 2


async def test_base_doc_defaults_to_latest_and_stays_fixed(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """06 T2.4 验收：上传 A 再上传 B，默认选 B；指定 A 选 A；
    启动后再上传 C，两个运行的底稿都不变。"""
    user_id = await sign_in(async_client, "base@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)  # 文档 A
    doc_b = await add_document(session_factory, user_id)

    default = (await start(async_client, inputs.user_job_id)).json()
    chosen = (
        await start(async_client, inputs.user_job_id, base_doc_id=str(inputs.doc_id))
    ).json()
    assert default["base_doc_id"] == str(doc_b)
    assert chosen["base_doc_id"] == str(inputs.doc_id)

    await add_document(session_factory, user_id)  # 文档 C
    await launcher.wait_all()
    for run, doc in ((default, doc_b), (chosen, inputs.doc_id)):
        result = (await async_client.get(f"/api/tailor-runs/{run['run_id']}")).json()
        assert result["base_doc_id"] == str(doc)


async def test_preconditions_are_checked_before_creating_a_run(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "pre@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id, sections=False)

    missing = await start(async_client, inputs.user_job_id)
    assert missing.status_code == 422
    assert set(missing.json()["detail"]["experiences"]) == {INTERN, PROJECT, "技能"}

    unknown_job = await start(async_client, UUID(int=1))
    assert unknown_job.status_code == 404
    assert await tailor_runs(session_factory, user_id) == 0


async def test_user_given_sections_unblock_legacy_facts(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "legacy@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id, sections=False)
    model.fact_ids = inputs.fact_ids
    sections = {INTERN: "internship", PROJECT: "project", "技能": "skill"}

    started = await start(async_client, inputs.user_job_id, sections=sections)
    assert started.status_code == 202, started.text
    await launcher.wait_all()
    result = (
        await async_client.get(f"/api/tailor-runs/{started.json()['run_id']}")
    ).json()
    assert result["status"] == "succeeded"
    assert [b["experience"] for b in result["blocks"]] == [INTERN, PROJECT, None]


# ---------- 幂等 ----------


async def tailor_runs(session_factory: Factory, user_id: UUID) -> int:
    async with session_factory() as session:
        return await session.scalar(
            select(func.count()).where(AiRun.user_id == user_id, AiRun.kind == "tailor")
        )


async def test_resend_with_same_key_returns_the_same_run(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """响应丢了、客户端重送：不新建运行、不再调用模型，返回原来的 202 和 run_id。"""
    user_id = await sign_in(async_client, "resend@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids

    first = await start(async_client, inputs.user_job_id, key="tailor-key-1")
    await launcher.wait_all()
    calls = dict(model.calls)
    again = await start(async_client, inputs.user_job_id, key="tailor-key-1")
    await launcher.wait_all()

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert await tailor_runs(session_factory, user_id) == 1
    assert model.calls == calls


async def test_same_key_with_other_request_is_rejected(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "rekey@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    assert (await start(async_client, inputs.user_job_id, key="k2")).status_code == 202
    other = await start(
        async_client, inputs.user_job_id, key="k2", base_doc_id=str(inputs.doc_id)
    )
    assert other.status_code == 422
    missing = await async_client.post(f"/api/jobs/{inputs.user_job_id}/tailor", json={})
    assert missing.status_code == 400
    await launcher.wait_all()
    assert await tailor_runs(session_factory, user_id) == 1


# ---------- 隔离 ----------


async def test_other_users_job_doc_and_run_are_404(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    owner = await sign_in(async_client, "owner@tailor-api.invalid")
    theirs = await seed_inputs(session_factory, owner)
    model.fact_ids = theirs.fact_ids
    run_id = (await start(async_client, theirs.user_job_id)).json()["run_id"]
    await launcher.wait_all()

    other = await sign_in(async_client, "other@tailor-api.invalid")
    mine = await seed_inputs(session_factory, other)
    assert (await async_client.get(f"/api/tailor-runs/{run_id}")).status_code == 404
    assert (await start(async_client, theirs.user_job_id)).status_code == 404
    with_their_doc = await start(
        async_client, mine.user_job_id, base_doc_id=str(theirs.doc_id)
    )
    assert with_their_doc.status_code == 404


# ---------- 终态 ----------


async def test_program_error_in_background_marks_run_failed(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(tailoring, "run_tailoring", broken)
    user_id = await sign_in(async_client, "bug@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)

    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()
    result = (await async_client.get(f"/api/tailor-runs/{run_id}")).json()
    assert result["status"] == "failed" and result["finished_at"] is not None


async def test_restart_fails_running_runs_but_keeps_waiting_ones(
    session_factory: Factory,
) -> None:
    user_id = await add_user(session_factory, "restart@tailor-api.invalid")
    async with session_factory.begin() as session:
        running = AiRun(user_id=user_id, kind="tailor")
        waiting = AiRun(user_id=user_id, kind="agent", status="waiting_confirmation")
        session.add_all([running, waiting])
    async with session_factory.begin() as session:
        assert await fail_abandoned_runs(session) == 1

    async with session_factory() as session:
        running = await session.get_one(AiRun, running.run_id)
        waiting = await session.get_one(AiRun, waiting.run_id)
    assert (running.status, running.stop_reason) == ("failed", "service_restarted")
    assert running.finished_at is not None
    assert waiting.status == RunStatus.WAITING_CONFIRMATION


# ---------- 完整简历 ----------


async def test_resume_edits_add_versions_and_stale_saves_conflict(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """06 T2.5 验收：编辑保存后仍能读旧版本；两个标签页都从 v1 开始，后保存的 409。"""
    user_id = await sign_in(async_client, "resume@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()

    v1 = (await async_client.get(f"/api/tailor-runs/{run_id}/resume")).json()
    assert v1["version"] == 1 and v1["facts"]
    content = copy.deepcopy(v1["content"])  # 不改动 v1 的数据，后面要和旧版本对照
    bullet = next(
        b for s in content["sections"] for e in s["entries"] for b in e["bullets"]
    )
    bullet["text"] = "主导" + bullet["text"]  # 第二层会提示角色升级
    bullet["fact_ids"] = []  # 客户端改引用无效

    saved = await async_client.put(
        f"/api/tailor-runs/{run_id}/resume",
        json={"base_version_id": v1["version_id"], "content": content},
    )
    assert saved.status_code == 200, saved.text
    v2 = saved.json()
    edited = next(
        b
        for s in v2["content"]["sections"]
        for e in s["entries"]
        for b in e["bullets"]
        if b["bullet_id"] == bullet["bullet_id"]
    )
    assert edited["edited"] and edited["fact_ids"]
    assert edited["check"]["layers"] == "program" and edited["check"]["issues"]

    stale = await async_client.put(
        f"/api/tailor-runs/{run_id}/resume",
        json={"base_version_id": v1["version_id"], "content": v1["content"]},
    )
    assert stale.status_code == 409
    old = (await async_client.get(f"/api/tailor-runs/{run_id}/resume?version=1")).json()
    assert old["content"] == v1["content"]
    current = (await async_client.get(f"/api/tailor-runs/{run_id}/resume")).json()
    assert current["version"] == 2


async def generated_resume(
    client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
    email: str,
) -> tuple[str, dict]:
    """登录并跑完一次改写，返回简历接口的地址和当前版本（v1）。"""
    user_id = await sign_in(client, email)
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    run_id = (await start(client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()
    url = f"/api/tailor-runs/{run_id}/resume"
    return url, (await client.get(url)).json()


def renamed(content: dict, tag: str) -> dict:
    """不改 v1 的数据：复制一份，在姓名后面加一个标记，用来分辨是哪一版。"""
    edited = copy.deepcopy(content)
    edited["header"][0] += tag
    return edited


async def test_save_response_is_the_version_it_created(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 存成 v2、回包之前 B 又存成 v3：A 收到的一定是 v2。
    收到 v3 的话，编辑器会把它当成自己的基准，下一次保存就盖掉 B 的修改。"""
    url, v1 = await generated_resume(
        async_client, session_factory, launcher, model, "save-race@tailor-api.invalid"
    )
    commit = AsyncSession.commit
    interleaved = False

    async def commit_then_let_another_tab_save(session: AsyncSession) -> None:
        """A 的事务一提交，简历行上的锁就放开了：这时另一个窗口马上存一版。"""
        nonlocal interleaved
        await commit(session)
        if interleaved:
            return
        interleaved = True
        current = (await async_client.get(url)).json()
        await async_client.put(
            url,
            json={
                "base_version_id": current["version_id"],
                "content": renamed(current["content"], "·B"),
            },
        )

    monkeypatch.setattr(AsyncSession, "commit", commit_then_let_another_tab_save)
    saved = await async_client.put(
        url,
        json={
            "base_version_id": v1["version_id"],
            "content": renamed(v1["content"], "·A"),
        },
    )
    assert saved.status_code == 200, saved.text

    mine = saved.json()
    assert (mine["version"], mine["current_version_id"]) == (2, mine["version_id"])
    assert [v["version"] for v in mine["versions"]] == [1, 2]
    assert mine["content"]["header"][0].endswith("·A")
    # A 拿自己收到的版本当基准再存：已经是旧版本，409，B 的修改不会被盖掉
    again = await async_client.put(
        url,
        json={
            "base_version_id": mine["version_id"],
            "content": renamed(mine["content"], "·A2"),
        },
    )
    assert again.status_code == 409
    current = (await async_client.get(url)).json()
    assert (current["version"], current["content"]["header"][0][-2:]) == (3, "·B")


async def test_preview_and_exports_download_from_the_saved_version(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    user_id = await sign_in(async_client, "export@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()

    preview = await async_client.get(f"/api/tailor-runs/{run_id}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("text/html")
    # 当前版本的预览可以编辑、保存到服务器；导出文件离线编辑
    assert f'"save_url": "/api/tailor-runs/{run_id}/resume"' in preview.text
    exported = await async_client.get(f"/api/tailor-runs/{run_id}/export")
    assert (
        'id="jobpilot-resume"' in exported.text and '"save_url": null' in exported.text
    )
    for kind, media in (
        ("html", "text/html"),
        ("md", "text/markdown"),
        ("docx", "application/vnd.openxmlformats"),
    ):
        response = await async_client.get(
            f"/api/tailor-runs/{run_id}/export", params={"format": kind, "version": 1}
        )
        assert response.status_code == 200, (kind, response.text)
        assert response.headers["content-type"].startswith(media)
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment; filename*=UTF-8''")
        assert disposition.endswith(f"-v1.{kind}")
    missing = await async_client.get(
        f"/api/tailor-runs/{run_id}/export", params={"version": 9}
    )
    assert missing.status_code == 404


PRIVATE_MARK = "ZZ_PENDING_MARK"


async def test_export_download_carries_only_visible_text(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    """下载接口给出的文件是要发出去的成品，打开源码也不能看到依据：
    待处理条目的原文和检查问题的说明、引用的事实 ID、检查结论都不在里面。"""
    user_id = await sign_in(async_client, "export-data@tailor-api.invalid")
    fact_id = UUID(int=7)
    passed = bullet_of(
        UUID(int=8), "完成接口开发", [UUID(int=9)], CheckStatus.PASSED, []
    )
    pending = bullet_of(
        UUID(int=10),
        f"{PRIVATE_MARK} 主导重构",
        [fact_id],
        CheckStatus.PENDING,
        [
            GroundingIssue(
                kind=IssueKind.ROLE,
                message=f"{PRIVATE_MARK} 职责高于事实",
                fragment="主导",
            )
        ],
    )
    content = ResumeContent(
        header=["测试同学"],
        sections=[
            ResumeSection(
                title="实习经历",
                kind=SectionKind.INTERNSHIP,
                entries=[
                    Entry(
                        entry_id="e1",
                        experience="示例实习",
                        heading="示例实习",
                        title="示例实习",
                        bullets=[passed, pending],
                    )
                ],
            )
        ],
    )
    async with session_factory.begin() as db:
        doc = ResumeDocument(user_id=user_id, raw_text="简历原文")
        fact_set = FactSet(user_id=user_id, version=1)
        db.add_all([doc, fact_set])
        await db.flush()
        _, run_id = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=fact_set.fact_set_id,
            job_title="后端实习",
            job_company="示例科技",
            version_number=1,
            created_at=datetime.now(UTC),
            content=content,
        )

    exported = await async_client.get(f"/api/tailor-runs/{run_id}/export")
    assert exported.status_code == 200
    for private in (PRIVATE_MARK, str(fact_id), str(UUID(int=9))):
        assert private not in exported.text
    # 离线编辑器和它的数据还在；数据里只有页面上看得见的那一条，只有 ID 和文字
    [entry] = [
        e for s in embedded(exported.text)["content"]["sections"] for e in s["entries"]
    ]
    assert entry["bullets"] == [
        {"bullet_id": str(passed.bullet_id), "text": "完成接口开发"}
    ]


async def test_old_version_preview_is_read_only(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """在旧版本上保存必然冲突，所以旧版本的预览不带编辑器。"""
    user_id = await sign_in(async_client, "oldpreview@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()
    v1 = (await async_client.get(f"/api/tailor-runs/{run_id}/resume")).json()
    saved = await async_client.put(
        f"/api/tailor-runs/{run_id}/resume",
        json={"base_version_id": v1["version_id"], "content": v1["content"]},
    )
    assert saved.status_code == 200

    old = await async_client.get(f"/api/tailor-runs/{run_id}/preview?version=1")
    current = await async_client.get(f"/api/tailor-runs/{run_id}/preview")
    assert 'id="jobpilot-resume"' not in old.text
    assert 'id="jobpilot-resume"' in current.text
