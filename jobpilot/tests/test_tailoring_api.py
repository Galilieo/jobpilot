"""改写接口：启动时固定底稿与输入、后台执行、查询结果、跨用户 404、段落类型待指定、
程序错误和服务重启后的终态。

数据直接写进数据库，只经由改写接口和登录接口调用，不依赖上传、确认等其他接口。
"""

import copy
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
    TailoredBullet,
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
from jobpilot.services.resume_versions import load_resume
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


RESUME_MARK = "ZZ_RESUME_TEXT_MARK"


@pytest.mark.parametrize(
    "failure", ["failing-write", "corrupt-snapshot", "plain-key-error"]
)
async def test_background_error_log_carries_no_resume_text(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """后台改写出错时，日志只留运行 ID、异常类名、错误码和调用栈位置。

    数据库异常的说明带着语句参数，约束错误还会由数据库带出整行内容，校验异常的说明
    带着输入值，KeyError 的说明带着缺失的键：简历正文会跟着进日志。
    三种真实的出错方式各在数据里放一个独特标记，抓取应用发出的全部日志。"""
    graph = tailoring.run_tailoring

    async def failing_write(deps, user_id, run_id, **kwargs) -> None:
        """落库时出错：条目引用了不存在的事实库版本，语句参数里带着简历正文。"""
        async with deps.session_factory() as session:
            session.add(
                TailoredBullet(
                    bullet_id=uuid4(),
                    run_id=run_id,
                    user_id=user_id,
                    fact_set_id=uuid4(),  # 不存在：外键拒绝
                    section="internship",
                    experience=RESUME_MARK,
                    order_no=0,
                    text=RESUME_MARK,
                    check_status="passed",
                    revision_count=0,
                    issues_json=[],
                )
            )
            await session.flush()

    async def corrupt_snapshot(deps, user_id, run_id, **kwargs) -> None:
        """岗位快照被写坏：真正的改写流程在读输入时校验失败，输入值里带着简历正文。"""
        async with deps.session_factory() as session:
            snapshot = await session.scalar(
                select(AiRun.job_snapshot_json).where(AiRun.run_id == run_id)
            )
        snapshot["requirements"] = RESUME_MARK  # 应该是列表
        async with deps.session_factory.begin() as session:
            await session.execute(
                update(AiRun)
                .where(AiRun.run_id == run_id)
                .values(job_snapshot_json=snapshot)
            )
        await graph(deps, user_id, run_id, **kwargs)

    async def plain_key_error(deps, user_id, run_id, **kwargs) -> None:
        """普通程序错误：KeyError 带着简历标记，日志里不能有它，但要看到函数名。"""
        raise KeyError(RESUME_MARK)

    broken = {
        "failing-write": failing_write,
        "corrupt-snapshot": corrupt_snapshot,
        "plain-key-error": plain_key_error,
    }[failure]
    monkeypatch.setattr(tailoring, "run_tailoring", broken)
    caplog.set_level(logging.DEBUG, logger="jobpilot")
    user_id = await sign_in(async_client, f"log-{failure}@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids

    run_id = (await start(async_client, inputs.user_job_id)).json()["run_id"]
    await launcher.wait_all()

    result = (await async_client.get(f"/api/tailor-runs/{run_id}")).json()
    assert result["status"] == "failed"
    assert RESUME_MARK not in caplog.text
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    expected_error, expected_funcs = {
        "failing-write": ("IntegrityError", ("failing_write",)),
        "corrupt-snapshot": ("ValidationError", ("corrupt_snapshot", "load_inputs")),
        "plain-key-error": ("KeyError", ("plain_key_error",)),
    }[failure]
    assert any(
        run_id in message
        and expected_error in message
        and all(
            f":{name}" in message or f" {name}" in message for name in expected_funcs
        )
        for message in errors
    ), errors


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


async def test_moving_a_bullet_to_another_entry_is_rejected(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """条目换了所属经历、文字没变：不能带着原来的通过结论存进去，422，也不新增版本。"""
    url, v1 = await generated_resume(
        async_client, session_factory, launcher, model, "moved@tailor-api.invalid"
    )
    content = copy.deepcopy(v1["content"])
    with_bullets = [
        e
        for s in content["sections"]
        for e in s["entries"]
        if e["bullets"] and e["experience"]
    ]
    source = next(
        e for e in with_bullets if any(b["origin"] == "passed" for b in e["bullets"])
    )
    target = next(e for e in with_bullets if e["experience"] != source["experience"])
    target["bullets"].append(source["bullets"].pop())

    rejected = await async_client.put(
        url, json={"base_version_id": v1["version_id"], "content": content}
    )

    assert rejected.status_code == 422, rejected.text
    assert any("条目换了所属经历" in p for p in rejected.json()["detail"])
    assert (await async_client.get(url)).json() == v1


async def test_reset_copies_the_generated_version_as_a_new_version(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """删空条目并保存后重置：得到一个和 v1 内容相同的新版本，删空的那一版留在历史里。"""
    url, v1 = await generated_resume(
        async_client, session_factory, launcher, model, "reset@tailor-api.invalid"
    )
    emptied = copy.deepcopy(v1["content"])
    for section in emptied["sections"]:
        for entry in section["entries"]:
            entry["bullets"] = []
    saved = await async_client.put(
        url, json={"base_version_id": v1["version_id"], "content": emptied}
    )
    assert saved.status_code == 200, saved.text
    base = saved.json()["version_id"]

    reset = await async_client.post(f"{url}/reset", json={"base_version_id": base})

    assert reset.status_code == 200, reset.text
    restored = reset.json()
    assert (restored["version"], restored["current_version_id"]) == (
        3,
        restored["version_id"],
    )
    assert restored["content"] == v1["content"]
    assert restored["facts"] == v1["facts"]
    assert [v["version"] for v in restored["versions"]] == [1, 2, 3]
    assert (await async_client.get(url)).json()["content"] == v1["content"]
    kept = (await async_client.get(url, params={"version": 2})).json()
    assert kept["content"] == emptied
    # 版本已经往前走了：再用旧的基准重置，409
    stale = await async_client.post(f"{url}/reset", json={"base_version_id": base})
    assert stale.status_code == 409


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
    editor = embedded(preview.text)
    assert (editor["save_url"], editor["reset_url"]) == (
        f"/api/tailor-runs/{run_id}/resume",
        f"/api/tailor-runs/{run_id}/resume/reset",
    )
    assert editor["export_url"] == f"/api/tailor-runs/{run_id}/export?format=html"
    # 编辑器按这个版本号导出（&version=n）：导出的是页面上看到的这一版
    assert editor["version"] == 1
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


async def test_load_resume_does_not_fetch_content_json_for_all_history(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    db_engine: AsyncEngine,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """读简历时历史列表只查元数据，只有选中的那一版才查正文。"""
    url, v1 = await generated_resume(
        async_client, session_factory, launcher, model, "history-sql@tailor-api.invalid"
    )
    run_id = UUID(url.split("/")[-2])
    base = v1["version_id"]
    for idx in range(2, 5):
        saved = await async_client.put(
            url,
            json={
                "base_version_id": base,
                "content": renamed(v1["content"], f"·v{idx}"),
            },
        )
        assert saved.status_code == 200
        base = saved.json()["version_id"]

    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with session_factory() as db:
            loaded = await load_resume(db, user_id, run_id)
            assert len(loaded.history) == 4
            assert loaded.version.version == 4

            statements.clear()
            no_hist = await load_resume(db, user_id, run_id, include_history=False)
            assert no_hist.history == []
            assert no_hist.version.version == 4
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)

    # 先验证 include_history=False 不会发任何没有主键/版本号过滤的 resume_version 查询
    version_queries = [s for s in statements if "FROM resume_version" in s]
    assert len(version_queries) == 1
    assert "resume_version_id =" in version_queries[0]

    # 再测完整 load_resume（含历史列表）
    statements.clear()
    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with session_factory() as db:
            await load_resume(db, user_id, run_id, version=2)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)

    version_queries = [s for s in statements if "FROM resume_version" in s]
    assert len(version_queries) == 2
    content_queries = [s for s in version_queries if "content_json" in s]
    history_queries = [s for s in version_queries if "content_json" not in s]
    assert len(content_queries) == 1 and "version =" in content_queries[0]
    assert len(history_queries) == 1
    for col in ("resume_version_id", "version", "created_at"):
        assert col in history_queries[0]


async def test_use_facts_replaces_bullet_records_rejection_and_enforces_guards(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """不用模型改写、改用事实原文：30%→20%、旧版不变、记录 user_decision、校验约束。"""
    from jobpilot.db.models import Fact, ResumeVersion

    user_id = await sign_in(async_client, "use-facts@tailor-api.invalid")
    inputs = await seed_inputs(session_factory, user_id)
    # 额外加一条「20%」事实，后续通过手改引入 30% 验证替换逻辑
    async with session_factory.begin() as db:
        twenty = Fact(
            user_id=user_id,
            fact_set_id=inputs.fact_set_id,
            lineage_id=uuid4(),
            experience=INTERN,
            section="internship",
            kind="result",
            responsibility="own",
            text="运单查询 P95 耗时降低 20%",
            has_number=True,
            source="user",
        )
        db.add(twenty)
        await db.flush()
        twenty_id = twenty.fact_id

    model.fact_ids = inputs.fact_ids
    started = await start(async_client, inputs.user_job_id)
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    await launcher.wait_all()

    url = f"/api/tailor-runs/{run_id}/resume"
    v1 = (await async_client.get(url)).json()
    bullet = next(
        b
        for s in v1["content"]["sections"]
        for e in s["entries"]
        if e["experience"] == INTERN
        for b in e["bullets"]
    )
    # 把 20% 事实加入条目引用，再手改为 30% 后测试改用事实原文
    async with session_factory.begin() as db:
        from jobpilot.db.models import BulletFact

        db.add(
            BulletFact(
                bullet_id=UUID(bullet["bullet_id"]),
                fact_id=twenty_id,
                user_id=user_id,
                fact_set_id=inputs.fact_set_id,
            )
        )
        ver1 = await db.get_one(ResumeVersion, UUID(v1["version_id"]))
        ver1_content = copy.deepcopy(ver1.content_json)
        for s in ver1_content["sections"]:
            for e in s["entries"]:
                for b in e["bullets"]:
                    if b["bullet_id"] == bullet["bullet_id"]:
                        b["fact_ids"].append(str(twenty_id))
        ver1.content_json = ver1_content

    v1 = (await async_client.get(url)).json()
    # 先手改 v1 成包含「30%」的文字作为 v2（尝试伪造 user_choice，服务端应忽略），
    # 再对其点「改用事实原文」
    v2_content = copy.deepcopy(v1["content"])
    for s in v2_content["sections"]:
        for e in s["entries"]:
            for b in e["bullets"]:
                if b["bullet_id"] == bullet["bullet_id"]:
                    b["text"] = "负责运单查询接口，耗时降低 30%"
                    b["user_choice"] = "use_facts"
    v2 = (
        await async_client.put(
            url, json={"base_version_id": v1["version_id"], "content": v2_content}
        )
    ).json()
    v2_bullet = v2["content"]["sections"][0]["entries"][0]["bullets"][0]
    assert "30%" in v2_bullet["text"]
    assert v2_bullet["user_choice"] is None

    # 预览页面内嵌的编辑器数据带 use_facts_url
    preview = await async_client.get(f"/api/tailor-runs/{run_id}/preview")
    assert embedded(preview.text)["use_facts_url"] == (
        f"/api/tailor-runs/{run_id}/resume/bullets/{{bullet_id}}/use-facts"
    )

    # 基准过期返回 409
    stale = await async_client.post(
        f"{url}/bullets/{bullet['bullet_id']}/use-facts",
        json={"base_version_id": v1["version_id"]},
    )
    assert stale.status_code == 409

    # 正常改用事实原文：v3 变为 20%，v2 保留 30%，user_decision='rejected'
    replaced = await async_client.post(
        f"{url}/bullets/{bullet['bullet_id']}/use-facts",
        json={"base_version_id": v2["version_id"]},
    )
    assert replaced.status_code == 200, replaced.text
    v3 = replaced.json()
    assert (v3["version"], v3["current_version_id"]) == (3, v3["version_id"])
    assert [item["version"] for item in v3["versions"]] == [1, 2, 3]
    v3_bullet = next(
        b
        for s in v3["content"]["sections"]
        for e in s["entries"]
        for b in e["bullets"]
        if b["bullet_id"] == bullet["bullet_id"]
    )
    assert "20%" in v3_bullet["text"] and "30%" not in v3_bullet["text"]
    assert v3_bullet["origin"] == "fallback" and v3_bullet["edited"] is False
    assert v3_bullet["user_choice"] == "use_facts"
    assert v3_bullet["check"]["text"] == v3_bullet["text"]

    # 导出只有 20%，旧版本 v2 仍是 30% 且 user_choice 为 None
    exported_v3 = await async_client.get(f"/api/tailor-runs/{run_id}/export")
    assert "20%" in exported_v3.text and "30%" not in exported_v3.text
    old_v2 = (await async_client.get(f"{url}?version=2")).json()
    old_v2_bullet = old_v2["content"]["sections"][0]["entries"][0]["bullets"][0]
    assert "30%" in old_v2_bullet["text"]
    assert old_v2_bullet["user_choice"] is None

    async with session_factory() as db:
        row = await db.get_one(TailoredBullet, UUID(bullet["bullet_id"]))
        assert row.user_decision == "rejected"

    # 引用的事实全失效时返回 422
    pending_id = uuid4()
    v3_with_pending = copy.deepcopy(v3["content"])
    v3_with_pending["sections"][0]["entries"][0]["bullets"].append(
        {
            "bullet_id": str(pending_id),
            "text": "待处理条目",
            "fact_ids": [],
            "origin": "pending",
            "edited": False,
            "check": {"text": "待处理条目", "layers": "full", "issues": []},
        }
    )
    # 往当前版本塞一条无有效事实的条目来测 422（同时需要 tailored_bullet 行）
    async with session_factory.begin() as db:
        db.add(
            TailoredBullet(
                bullet_id=pending_id,
                run_id=UUID(run_id),
                user_id=user_id,
                fact_set_id=inputs.fact_set_id,
                section="experience",
                experience=INTERN,
                order_no=99,
                text="待处理条目",
                check_status="pending",
                revision_count=0,
                issues_json=[],
            )
        )
        ver = await db.get_one(ResumeVersion, UUID(v3["version_id"]))
        ver.content_json = v3_with_pending
    empty_facts = await async_client.post(
        f"{url}/bullets/{pending_id}/use-facts",
        json={"base_version_id": v3["version_id"]},
    )
    assert empty_facts.status_code == 422

    # 换另一个用户请求别人的条目 404；别人带着自己的 run + 我的 bullet_id 也 404
    other_url, other_v1 = await generated_resume(
        async_client, session_factory, launcher, model, "other-use@tailor-api.invalid"
    )
    assert (
        await async_client.post(
            f"{url}/bullets/{bullet['bullet_id']}/use-facts",
            json={"base_version_id": v3["version_id"]},
        )
    ).status_code == 404
    assert (
        await async_client.post(
            f"{other_url}/bullets/{bullet['bullet_id']}/use-facts",
            json={"base_version_id": other_v1["version_id"]},
        )
    ).status_code == 404


async def test_font_codes_legacy_readback_and_preview_csp(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
    model: FakeModel,
) -> None:
    """字体只接受固定代码；旧字体串读回对应代码但不改历史数据；预览带 CSP、导出不带。"""
    from jobpilot.db.models import ResumeVersion

    url, v1 = await generated_resume(
        async_client, session_factory, launcher, model, "font-csp@tailor-api.invalid"
    )
    run_id = url.split("/")[-2]

    # 任意字符串（包括 CSS 注入片段）被 422 拒绝
    bad_content = copy.deepcopy(v1["content"])
    bad_content["style"]["font"] = '"PingFang SC"; } body { display: none'
    rejected = await async_client.put(
        url, json={"base_version_id": v1["version_id"], "content": bad_content}
    )
    assert rejected.status_code == 422

    # 合法代码可以保存
    good_content = copy.deepcopy(v1["content"])
    good_content["style"]["font"] = "songti"
    saved = await async_client.put(
        url, json={"base_version_id": v1["version_id"], "content": good_content}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["content"]["style"]["font"] == "songti"

    # 历史版本里存的旧字体串读回对应代码（认不出的回默认），数据库原行不变
    async with session_factory.begin() as db:
        ver1 = await db.get_one(ResumeVersion, UUID(v1["version_id"]))
        legacy_json = copy.deepcopy(ver1.content_json)
        legacy_json["style"]["font"] = '"Kaiti SC", KaiTi, serif'
        ver1.content_json = legacy_json
        ver2 = await db.get_one(ResumeVersion, UUID(saved.json()["version_id"]))
        unknown_json = copy.deepcopy(ver2.content_json)
        unknown_json["style"]["font"] = "UnknownFontFamily"
        ver2.content_json = unknown_json

    read_v1 = (await async_client.get(f"{url}?version=1")).json()
    assert read_v1["content"]["style"]["font"] == "kaiti"
    read_v2 = (await async_client.get(f"{url}?version=2")).json()
    assert read_v2["content"]["style"]["font"] == "default"
    # 数据库历史数据未被修改
    async with session_factory() as db:
        row1 = await db.get_one(ResumeVersion, UUID(v1["version_id"]))
        assert row1.content_json["style"]["font"] == '"Kaiti SC", KaiTi, serif'

    # 预览响应带 Content-Security-Policy 且 nonce 与两个脚本一致；导出不带 CSP
    preview = await async_client.get(f"/api/tailor-runs/{run_id}/preview")
    assert preview.status_code == 200
    csp = preview.headers.get("content-security-policy")
    assert csp is not None
    assert "connect-src 'self'" in csp
    assert "default-src 'none'" in csp
    match = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert match is not None
    nonce = match.group(1)
    assert preview.text.count(f'<script nonce="{nonce}">') == 2
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]

    exported = await async_client.get(f"/api/tailor-runs/{run_id}/export?format=html")
    assert exported.status_code == 200
    assert "content-security-policy" not in exported.headers
