"""匹配报告接口集成测试：覆盖只用已发布事实、表达缺口、快照固定与安全校验。

对应 06 T2.1 的全部验收情景（只改 JD 事实不变、30% 改 20% 且改 JD、拒绝其他版本）。
隔离（A 碰不到 B 的报告和匹配接口）由 test_isolation.py 覆盖。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api.deps import get_model_client
from jobpilot.db.models import AiRun, CallOutcome, MatchReport, ModelCall, RunStatus
from jobpilot.main import app
from jobpilot.tests.conftest import EMPTY_JOB
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream
from jobpilot.tests.test_facts_api import EXTRACTION, RESUME

JOB_RAW = (
    "星桥数据 · 后端开发实习生\n"
    "1. 熟悉后端开发与查询优化；\n"
    "2. 熟悉 Redis 缓存（加分）。\n"
)
JOB_PARSED = {
    "basic_fields": {"title": "后端开发实习生", "company": "星桥数据"},
    "requirements": [
        {
            "type": "hard",
            "text": "熟悉后端开发与查询优化",
            "source_quote": "熟悉后端开发与查询优化",
        },
        {
            "type": "preferred",
            "text": "熟悉 Redis 缓存",
            "source_quote": "熟悉 Redis 缓存",
        },
    ],
    "duties": [],
    "keywords": ["Redis"],
}


async def _sign_in(client: httpx.AsyncClient, email: str) -> httpx.AsyncClient:
    credentials = {"email": email, "password": "match-api-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success
    return client


@pytest.fixture
async def signed_in(async_client: httpx.AsyncClient) -> httpx.AsyncClient:
    return await _sign_in(async_client, "match-api@example.invalid")


@pytest.fixture
async def captured_model(
    async_client: httpx.AsyncClient,
) -> AsyncIterator[tuple[list[str], list[dict[str, Any]]]]:
    """记录实际外发给模型的 HTTP JSON，并按预设队列返回响应。"""
    outputs: list[str] = []
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if outputs:
            return completion_response(outputs.pop(0))
        if b'label=\\"jd\\"' in request.content:
            return completion_response(EMPTY_JOB)
        return completion_response('{"experiences": []}')

    async with fake_upstream(respond) as client:
        app.dependency_overrides[get_model_client] = lambda: client
        yield outputs, requests


async def _publish_resume_facts(
    client: httpx.AsyncClient, outputs: list[str]
) -> dict[str, Any]:
    outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    uploaded = await client.post("/api/resumes", json={"raw_text": RESUME})
    assert uploaded.status_code == 201, uploaded.text
    for item in uploaded.json()["items"]:
        confirmed = await client.put(
            f"/api/facts/{item['lineage_id']}",
            json={"confirmed": True, "responsibility": "own"},
        )
        assert confirmed.status_code == 200, confirmed.text
    committed = await client.post("/api/facts/commit", json={"base_fact_set_id": None})
    assert committed.status_code == 200, committed.text
    return (await client.get("/api/facts")).json()


async def _add_user_fact(
    client: httpx.AsyncClient,
    text: str,
    *,
    base_fact_set_id: str | None,
    commit: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    added = await client.post(
        "/api/facts",
        json={
            "experience": "课外项目",
            "kind": "action",
            "responsibility": "own",
            "text": text,
        },
    )
    assert added.status_code == 201, added.text
    fact_set = None
    if commit:
        committed = await client.post(
            "/api/facts/commit", json={"base_fact_set_id": base_fact_set_id}
        )
        assert committed.status_code == 200, committed.text
        fact_set = committed.json()
    return added.json(), fact_set


async def _add_job(
    client: httpx.AsyncClient, outputs: list[str], raw_text: str = JOB_RAW
) -> dict[str, Any]:
    outputs.append(json.dumps(JOB_PARSED, ensure_ascii=False))
    response = await client.post("/api/jobs", json={"raw_text": raw_text})
    assert response.status_code == 201, response.text
    return response.json()


async def test_match_persists_report_separates_summaries_and_outputs_no_score(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """正常生成报告：持久化运行与快照、硬性/加分分开汇总、不输出总分、可回读详情和列表。"""
    lib = await _publish_resume_facts(signed_in, model_outputs)
    resume_fact = lib["facts"][0]
    job = await _add_job(signed_in, model_outputs)
    r_hard, r_pref = job["parsed"]["requirements"]

    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_hard["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [resume_fact["fact_id"]],
                        "reason": "在课程排班面板中独立优化了查询",
                        "suggestion": "突出索引优化过程",
                    },
                    {
                        "requirement_id": r_pref["requirement_id"],
                        "verdict": "missing",
                        "fact_ids": [],
                        "reason": "未见 Redis 经验",
                        "suggestion": "建议学习并补做相关缓存项目",
                    },
                ],
                "main_gaps": ["缺少 Redis 缓存经验（加分项）"],
            },
            ensure_ascii=False,
        )
    )

    resp = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert resp.status_code == 201, resp.text
    report = resp.json()

    assert set(report) == {
        "report_id",
        "run_id",
        "job_id",
        "fact_set_id",
        "fact_set_version",
        "hard_summary",
        "preferred_summary",
        "main_gaps",
        "items",
        "created_at",
    }
    assert report["fact_set_id"] == lib["fact_set"]["fact_set_id"]
    assert report["fact_set_version"] == 1
    assert report["hard_summary"] == {
        "total": 1,
        "covered": 1,
        "expression_gap": 0,
        "partial": 0,
        "missing": 0,
    }
    assert report["preferred_summary"] == {
        "total": 1,
        "covered": 0,
        "expression_gap": 0,
        "partial": 0,
        "missing": 1,
    }
    assert [i["verdict"] for i in report["items"]] == ["covered", "missing"]
    assert report["items"][0]["facts"][0]["fact_id"] == resume_fact["fact_id"]
    assert report["main_gaps"] == ["缺少 Redis 缓存经验（加分项）"]

    detail = await signed_in.get(f"/api/match-reports/{report['report_id']}")
    assert detail.status_code == 200
    assert detail.json() == report

    history = await signed_in.get(f"/api/jobs/{job['job_id']}/matches")
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["report_id"] == report["report_id"]

    async with session_factory() as db:
        runs = (await db.scalars(select(AiRun).where(AiRun.kind == "match"))).all()
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCEEDED
        assert str(runs[0].fact_set_id) == lib["fact_set"]["fact_set_id"]
        assert str(runs[0].user_job_id) == job["job_id"]
        assert runs[0].job_snapshot_json is not None
        assert len((await db.scalars(select(MatchReport))).all()) == 1


async def test_user_only_fact_covered_becomes_expression_gap(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """模型判为 covered 但引用全部来自用户补充时，程序自动改判为 expression_gap。"""
    lib = await _publish_resume_facts(signed_in, model_outputs)
    _, v2 = await _add_user_fact(
        signed_in,
        "用 Redis 做过分布式锁和热点缓存",
        base_fact_set_id=lib["fact_set"]["fact_set_id"],
        commit=True,
    )
    assert v2 is not None and v2["version"] == 2
    latest = (await signed_in.get("/api/facts")).json()
    resume_fact = next(f for f in latest["facts"] if f["source"] == "resume")
    user_fact = next(f for f in latest["facts"] if f["source"] == "user")

    job = await _add_job(signed_in, model_outputs)
    r_hard, r_pref = job["parsed"]["requirements"]
    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_hard["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [resume_fact["fact_id"]],
                        "reason": "简历已写",
                        "suggestion": "保留",
                    },
                    {
                        "requirement_id": r_pref["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [user_fact["fact_id"]],
                        "reason": "用户做过 Redis，但当前简历没写",
                        "suggestion": "改写时补进简历",
                    },
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )

    resp = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert [i["verdict"] for i in report["items"]] == ["covered", "expression_gap"]
    assert report["preferred_summary"]["expression_gap"] == 1
    assert report["preferred_summary"]["covered"] == 0


async def test_matching_uses_only_published_facts_and_ignores_draft(
    signed_in: httpx.AsyncClient,
    captured_model: tuple[list[str], list[dict[str, Any]]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """只使用已发布事实库：仅有草稿时 422；有版本时只发已发布事实并拒绝草稿引用。"""
    outputs, requests = captured_model
    job = await _add_job(signed_in, outputs)
    r_hard, r_pref = job["parsed"]["requirements"]

    # 1. 仅有未发布的草稿条目 -> 422 且不调用模型
    await _add_user_fact(
        signed_in, "草稿里的高并发实践", base_fact_set_id=None, commit=False
    )
    requests.clear()
    blocked = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert blocked.status_code == 422
    assert requests == []

    # 2. 发布 v1 后，再往草稿新增一条未发布的事实 B
    v1_set = (
        await signed_in.post("/api/facts/commit", json={"base_fact_set_id": None})
    ).json()
    v1_fact = (await signed_in.get("/api/facts")).json()["facts"][0]
    draft_item, _ = await _add_user_fact(
        signed_in,
        "未发布的 Rust 微服务经验",
        base_fact_set_id=v1_set["fact_set_id"],
        commit=False,
    )

    # 先验证正常运行时外发给模型的只有 v1 事实，绝不包含草稿里的事实 B
    requests.clear()
    outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_hard["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [v1_fact["fact_id"]],
                        "reason": "满足",
                        "suggestion": "突出",
                    },
                    {
                        "requirement_id": r_pref["requirement_id"],
                        "verdict": "missing",
                        "fact_ids": [],
                        "reason": "未提及",
                        "suggestion": "建议学习",
                    },
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    ok_resp = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert ok_resp.status_code == 201, ok_resp.text
    assert len(requests) == 1
    sent_text = json.dumps(requests[0], ensure_ascii=False)
    assert v1_fact["fact_id"] in sent_text
    assert "未发布的 Rust 微服务经验" not in sent_text

    # 再验证若模型强行引用草稿中的 lineage_id，被程序拒绝并记失败
    outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_hard["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [draft_item["lineage_id"]],
                        "reason": "引用了未发布草稿",
                        "suggestion": "无",
                    },
                    {
                        "requirement_id": r_pref["requirement_id"],
                        "verdict": "missing",
                        "fact_ids": [],
                        "reason": "未提及",
                        "suggestion": "建议学习",
                    },
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    rejected = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert rejected.status_code == 502
    async with session_factory() as db:
        reports = (await db.scalars(select(MatchReport))).all()
        assert len(reports) == 1  # 只有成功的那一次
        runs = (
            await db.scalars(
                select(AiRun).where(AiRun.kind == "match").order_by(AiRun.started_at)
            )
        ).all()
        assert [r.status for r in runs] == [RunStatus.SUCCEEDED, RunStatus.FAILED]


async def test_match_rejects_job_without_requirements(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _publish_resume_facts(signed_in, model_outputs)
    created = await signed_in.post("/api/jobs", json={"raw_text": "空要求岗位"})
    assert created.status_code == 201
    job_id = created.json()["job_id"]

    resp = await signed_in.post(f"/api/jobs/{job_id}/match")
    assert resp.status_code == 422
    async with session_factory() as db:
        runs = (await db.scalars(select(AiRun).where(AiRun.kind == "match"))).all()
        assert runs == []


async def test_editing_jd_preserves_old_report_snapshot(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """验收情景 1：只修改岗位要求、事实库不变时，旧报告保留旧要求，新报告用新要求。"""
    lib = await _publish_resume_facts(signed_in, model_outputs)
    fact_id = lib["facts"][0]["fact_id"]

    # 创建只带一条旧要求的岗位
    created = await signed_in.post("/api/jobs", json={"raw_text": "后端岗位"})
    job_id = created.json()["job_id"]
    put_old = await signed_in.put(
        f"/api/jobs/{job_id}",
        json={"requirements": [{"type": "hard", "text": "熟悉 MySQL"}]},
    )
    r_old = put_old.json()["parsed"]["requirements"][0]

    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_old["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [fact_id],
                        "reason": "满足旧要求",
                        "suggestion": "保留",
                    }
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    report_1 = (await signed_in.post(f"/api/jobs/{job_id}/match")).json()

    # 修改岗位要求为两条新要求，事实库不变
    put_new = await signed_in.put(
        f"/api/jobs/{job_id}",
        json={
            "requirements": [
                {"type": "hard", "text": "熟悉 PostgreSQL"},
                {"type": "hard", "text": "熟悉 Redis"},
            ]
        },
    )
    r_new_1, r_new_2 = put_new.json()["parsed"]["requirements"]

    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_new_1["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [fact_id],
                        "reason": "满足新要求一",
                        "suggestion": "突出",
                    },
                    {
                        "requirement_id": r_new_2["requirement_id"],
                        "verdict": "missing",
                        "fact_ids": [],
                        "reason": "缺 Redis",
                        "suggestion": "建议学习 Redis",
                    },
                ],
                "main_gaps": ["缺少 Redis"],
            },
            ensure_ascii=False,
        )
    )
    report_2 = (await signed_in.post(f"/api/jobs/{job_id}/match")).json()

    reread_1 = (
        await signed_in.get(f"/api/match-reports/{report_1['report_id']}")
    ).json()
    reread_2 = (
        await signed_in.get(f"/api/match-reports/{report_2['report_id']}")
    ).json()

    assert [i["requirement_text"] for i in reread_1["items"]] == ["熟悉 MySQL"]
    assert reread_1["fact_set_version"] == 1
    assert [i["requirement_text"] for i in reread_2["items"]] == [
        "熟悉 PostgreSQL",
        "熟悉 Redis",
    ]
    assert reread_2["fact_set_version"] == 1

    history = (await signed_in.get(f"/api/jobs/{job_id}/matches")).json()
    assert [h["report_id"] for h in history] == [
        report_2["report_id"],
        report_1["report_id"],
    ]
    assert [h["hard_summary"]["total"] for h in history] == [2, 1]


async def test_updating_fact_from_30_to_20_and_editing_jd_preserves_v3_report(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """验收情景 2：事实从 30% 改 20%（v3 -> v4）并改 JD，旧报告保留 v3 与旧要求。"""
    # 建至 v3：先发布 v1、v2，再把目标事实写入 v3（含 30%）
    v1_item, v1_set = await _add_user_fact(
        signed_in, "掌握 Python 后端开发", base_fact_set_id=None, commit=True
    )
    assert v1_set is not None
    _, v2_set = await _add_user_fact(
        signed_in,
        "掌握 FastAPI 框架",
        base_fact_set_id=v1_set["fact_set_id"],
        commit=True,
    )
    assert v2_set is not None
    perf_draft, v3_set = await _add_user_fact(
        signed_in,
        "优化查询后接口耗时降低 30%",
        base_fact_set_id=v2_set["fact_set_id"],
        commit=True,
    )
    assert v3_set is not None and v3_set["version"] == 3

    v3_facts = (await signed_in.get("/api/facts")).json()["facts"]
    perf_v3 = next(f for f in v3_facts if f["lineage_id"] == perf_draft["lineage_id"])
    assert "30%" in perf_v3["text"]

    # 创建岗位并做 v3 匹配
    job_resp = await signed_in.post("/api/jobs", json={"raw_text": "性能优化岗位"})
    job_id = job_resp.json()["job_id"]
    put_v3 = await signed_in.put(
        f"/api/jobs/{job_id}",
        json={"requirements": [{"type": "hard", "text": "有接口性能优化经验"}]},
    )
    req_v3 = put_v3.json()["parsed"]["requirements"][0]

    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": req_v3["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [perf_v3["fact_id"]],
                        "reason": "v3 记录耗时降低 30%",
                        "suggestion": "写入简历",
                    }
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    report_v3 = (await signed_in.post(f"/api/jobs/{job_id}/match")).json()

    # 修订同一条事实为 20% 并发布为 v4，同时修改 JD 要求
    edited = await signed_in.put(
        f"/api/facts/{perf_draft['lineage_id']}",
        json={"text": "优化查询后接口耗时降低 20%", "confirmed": True},
    )
    assert edited.status_code == 200
    v4_set = (
        await signed_in.post(
            "/api/facts/commit", json={"base_fact_set_id": v3_set["fact_set_id"]}
        )
    ).json()
    assert v4_set["version"] == 4
    v4_facts = (await signed_in.get("/api/facts")).json()["facts"]
    perf_v4 = next(f for f in v4_facts if f["lineage_id"] == perf_draft["lineage_id"])
    assert perf_v4["fact_id"] != perf_v3["fact_id"]

    put_v4 = await signed_in.put(
        f"/api/jobs/{job_id}",
        json={"requirements": [{"type": "hard", "text": "接口延迟优化经验"}]},
    )
    req_v4 = put_v4.json()["parsed"]["requirements"][0]

    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": req_v4["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [perf_v4["fact_id"]],
                        "reason": "v4 记录耗时降低 20%",
                        "suggestion": "写入简历",
                    }
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    report_v4 = (await signed_in.post(f"/api/jobs/{job_id}/match")).json()

    # 回读旧报告：仍显示 v3、30% 与旧要求；新报告显示 v4、20% 与新要求
    reread_v3 = (
        await signed_in.get(f"/api/match-reports/{report_v3['report_id']}")
    ).json()
    reread_v4 = (
        await signed_in.get(f"/api/match-reports/{report_v4['report_id']}")
    ).json()

    assert reread_v3["fact_set_version"] == 3
    assert reread_v3["items"][0]["requirement_text"] == "有接口性能优化经验"
    assert reread_v3["items"][0]["facts"][0]["text"] == "优化查询后接口耗时降低 30%"
    assert reread_v4["fact_set_version"] == 4
    assert reread_v4["items"][0]["requirement_text"] == "接口延迟优化经验"
    assert reread_v4["items"][0]["facts"][0]["text"] == "优化查询后接口耗时降低 20%"


async def test_citing_other_version_fact_id_of_same_user_is_rejected(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同一用户发布 v2 后，匹配若引用旧版本 v1 的 fact_id，被拒绝且不重试修复。"""
    item, v1 = await _add_user_fact(
        signed_in, "旧版本 v1 事实", base_fact_set_id=None, commit=True
    )
    assert v1 is not None
    v1_fact_id = (await signed_in.get("/api/facts")).json()["facts"][0]["fact_id"]

    await signed_in.put(
        f"/api/facts/{item['lineage_id']}",
        json={"text": "新版本 v2 事实", "confirmed": True},
    )
    v2 = (
        await signed_in.post(
            "/api/facts/commit", json={"base_fact_set_id": v1["fact_set_id"]}
        )
    ).json()
    assert v2["version"] == 2

    job = await _add_job(signed_in, model_outputs)
    r_hard, r_pref = job["parsed"]["requirements"]
    model_outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": r_hard["requirement_id"],
                        "verdict": "covered",
                        "fact_ids": [v1_fact_id],
                        "reason": "错引了 v1 的 fact_id",
                        "suggestion": "无",
                    },
                    {
                        "requirement_id": r_pref["requirement_id"],
                        "verdict": "missing",
                        "fact_ids": [],
                        "reason": "缺",
                        "suggestion": "建议学习",
                    },
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )

    resp = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert resp.status_code == 502

    async with session_factory() as db:
        assert (await db.scalars(select(MatchReport))).all() == []
        [run] = (await db.scalars(select(AiRun).where(AiRun.kind == "match"))).all()
        assert run.status == RunStatus.FAILED
        calls = (
            await db.scalars(select(ModelCall).where(ModelCall.run_id == run.run_id))
        ).all()
        assert len(calls) == 1
        assert calls[0].outcome == CallOutcome.INVALID_OUTPUT


async def test_invalid_json_repaired_once_and_double_failure_marks_failed(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """第一次输出非 JSON 时经网关修复一次后成功；连续两次非法则 502 且不保存报告。"""
    lib = await _publish_resume_facts(signed_in, model_outputs)
    fact_id = lib["facts"][0]["fact_id"]
    job = await _add_job(signed_in, model_outputs)
    r_hard, r_pref = job["parsed"]["requirements"]

    valid_json = json.dumps(
        {
            "items": [
                {
                    "requirement_id": r_hard["requirement_id"],
                    "verdict": "covered",
                    "fact_ids": [fact_id],
                    "reason": "满足",
                    "suggestion": "保留",
                },
                {
                    "requirement_id": r_pref["requirement_id"],
                    "verdict": "missing",
                    "fact_ids": [],
                    "reason": "缺",
                    "suggestion": "建议学习",
                },
            ],
            "main_gaps": [],
        },
        ensure_ascii=False,
    )
    model_outputs.extend(["不是 JSON", valid_json])

    repaired = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert repaired.status_code == 201, repaired.text
    repaired_run_id = repaired.json()["run_id"]

    async with session_factory() as db:
        calls = (
            await db.scalars(
                select(ModelCall)
                .where(ModelCall.run_id == repaired_run_id)
                .order_by(ModelCall.created_at)
            )
        ).all()
        assert [c.outcome for c in calls] == [
            CallOutcome.INVALID_OUTPUT,
            CallOutcome.SUCCESS,
        ]
        assert calls[0].prompt_version == "match_job@1"
        assert calls[1].prompt_version == "match_job@1+repair_json@1"

    model_outputs.extend(["还是坏的", "仍然坏的"])
    failed = await signed_in.post(f"/api/jobs/{job['job_id']}/match")
    assert failed.status_code == 502
    async with session_factory() as db:
        assert len((await db.scalars(select(MatchReport))).all()) == 1
        runs = (
            await db.scalars(
                select(AiRun).where(AiRun.kind == "match").order_by(AiRun.started_at)
            )
        ).all()
        assert [r.status for r in runs] == [RunStatus.SUCCEEDED, RunStatus.FAILED]


async def test_outgoing_match_request_wraps_untrusted_text_and_redacts_pii(
    signed_in: httpx.AsyncClient,
    captured_model: tuple[list[str], list[dict[str, Any]]],
) -> None:
    """外发给模型的岗位要求与事实必须经过不可信区块包装并遮蔽联系方式。"""
    outputs, requests = captured_model
    await _add_user_fact(
        signed_in,
        "联系导师 13812345678 完成高并发系统上线",
        base_fact_set_id=None,
        commit=True,
    )
    fact_id = (await signed_in.get("/api/facts")).json()["facts"][0]["fact_id"]

    job_resp = await signed_in.post("/api/jobs", json={"raw_text": "包含联系方式的 JD"})
    job_id = job_resp.json()["job_id"]
    put_resp = await signed_in.put(
        f"/api/jobs/{job_id}",
        json={
            "requirements": [
                {"type": "hard", "text": "有高并发经验，详情联系 hr@secret.invalid"}
            ]
        },
    )
    req_id = put_resp.json()["parsed"]["requirements"][0]["requirement_id"]

    requests.clear()
    outputs.append(
        json.dumps(
            {
                "items": [
                    {
                        "requirement_id": req_id,
                        "verdict": "covered",
                        "fact_ids": [fact_id],
                        "reason": "做过高并发系统",
                        "suggestion": "突出压测细节",
                    }
                ],
                "main_gaps": [],
            },
            ensure_ascii=False,
        )
    )
    resp = await signed_in.post(f"/api/jobs/{job_id}/match")
    assert resp.status_code == 201, resp.text

    assert len(requests) == 1
    body_text = json.dumps(requests[0], ensure_ascii=False)
    user_content = requests[0]["messages"][1]["content"]
    assert "13812345678" not in body_text
    assert "hr@secret.invalid" not in body_text
    assert "[PHONE]" in body_text
    assert "[EMAIL]" in body_text
    assert '<untrusted_data label="job_requirements">' in user_content
    assert '<untrusted_data label="facts">' in user_content
