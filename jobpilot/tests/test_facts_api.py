"""简历上传与事实库接口：用假模型走通「提取 → 上传拆分 → 确认 → 发布」。

隔离（A 碰不到 B 的事实和简历）由 test_isolation.py 覆盖。
"""

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import AiRun, ResumeDocument, RunStatus

RESUME = (
    "项目｜课程排班面板\n负责实现按周次筛选课程的功能\n优化查询后响应时间降低 30%\n"
)
EXTRACTION = {
    "experiences": [
        {
            "name": "课程排班面板",
            "section": "project",
            "heading_quote": "项目｜课程排班面板",
            "facts": [
                {
                    "kind": "action",
                    "responsibility": "own",
                    "responsibility_stated": True,
                    "text": "实现按周次筛选课程的功能",
                    "has_number": False,
                    "source_quote": "负责实现按周次筛选课程的功能",
                },
                {
                    "kind": "result",
                    "responsibility": "participate",
                    "responsibility_stated": False,
                    "text": "优化查询后响应时间降低 30%",
                    "has_number": True,
                    "source_quote": "优化查询后响应时间降低 30%",
                },
            ],
        }
    ]
}


@pytest.fixture
async def signed_in(async_client: httpx.AsyncClient) -> httpx.AsyncClient:
    credentials = {"email": "facts-api@example.invalid", "password": "facts-api-1"}
    assert (await async_client.post("/api/auth/register", json=credentials)).is_success
    assert (await async_client.post("/api/auth/login", json=credentials)).is_success
    return async_client


# ---------- 提取文字 ----------


async def test_extract_returns_text_without_saving(
    signed_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await signed_in.post(
        "/api/resumes/extract",
        files={"file": ("resume.txt", RESUME.encode(), "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": RESUME.rstrip("\n")}
    async with session_factory() as db:
        assert (await db.scalars(select(ResumeDocument))).all() == []


async def test_extract_sends_images_to_ocr(signed_in: httpx.AsyncClient) -> None:
    response = await signed_in.post(
        "/api/resumes/extract",
        files={"file": ("resume.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )

    assert response.status_code == 422
    assert "/api/ocr" in response.json()["detail"]


# ---------- 上传、确认、发布 ----------


async def test_upload_confirm_and_publish(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))

    uploaded = await signed_in.post(
        "/api/resumes", json={"filename": "resume.txt", "raw_text": RESUME}
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    clean, flagged = body["items"]
    assert {clean["status"], flagged["status"]} == {"pending"}
    assert flagged["review_reasons"] == ["responsibility_unstated"]

    doc = (await signed_in.get(f"/api/resumes/{body['doc_id']}")).json()
    [section] = doc["sections"]
    assert doc["raw_text"][section["start"] : section["end"]].startswith("项目")
    assert (
        doc["raw_text"][clean["span_start"] : clean["span_end"]]
        == (clean["source_quote"])
    )

    confirmed = await signed_in.post(
        "/api/facts/batch", json={"action": "confirm_clean"}
    )
    assert confirmed.json() == {"changed": 1}

    published = await signed_in.post(
        "/api/facts/commit", json={"base_fact_set_id": None}
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 1

    library = (await signed_in.get("/api/facts")).json()
    assert [f["lineage_id"] for f in library["facts"]] == [clean["lineage_id"]]
    assert [i["lineage_id"] for i in library["draft"]["items"]] == [
        flagged["lineage_id"]
    ]

    # 还拿着旧基准（空）再发布：版本已变，409。
    stale = await signed_in.post("/api/facts/commit", json={"base_fact_set_id": None})
    assert stale.status_code == 409
    # 基准正确但没有改动：400。
    nothing = await signed_in.post(
        "/api/facts/commit",
        json={"base_fact_set_id": published.json()["fact_set_id"]},
    )
    assert nothing.status_code == 400


async def test_set_responsibility_then_confirm_clean(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """回答「这段经历里没写职责的是你负责的吗」，之后一键确认就能覆盖它们。"""
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    await signed_in.post("/api/resumes", json={"raw_text": RESUME})

    answered = await signed_in.post(
        "/api/facts/batch",
        json={
            "action": "set_responsibility",
            "experience": "课程排班面板",
            "responsibility": "own",
        },
    )
    confirmed = await signed_in.post(
        "/api/facts/batch", json={"action": "confirm_clean"}
    )

    assert answered.json() == {"changed": 1}
    assert confirmed.json() == {"changed": 2}


async def test_quote_failure_blocks_confirmation(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    bad = json.loads(json.dumps(EXTRACTION))
    bad["experiences"][0]["facts"][0]["source_quote"] = "原文里没有这一句"
    model_outputs.append(json.dumps(bad, ensure_ascii=False))
    [blocked, _] = (
        await signed_in.post("/api/resumes", json={"raw_text": RESUME})
    ).json()["items"]

    response = await signed_in.put(
        f"/api/facts/{blocked['lineage_id']}", json={"confirmed": True}
    )

    assert response.status_code == 422
    assert "修改文字或删除" in response.json()["detail"]


async def test_model_failure_marks_run_failed_and_keeps_resume(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    model_outputs.append("不是 JSON")
    model_outputs.append("还不是 JSON")  # JSON 修复重发也失败

    response = await signed_in.post("/api/resumes", json={"raw_text": RESUME})

    assert response.status_code == 502
    async with session_factory() as db:
        [run] = (await db.scalars(select(AiRun))).all()
        assert run.status == RunStatus.FAILED
        assert len((await db.scalars(select(ResumeDocument))).all()) == 1
    library = (await signed_in.get("/api/facts")).json()
    assert library["draft"]["items"] == []


async def test_requests_cannot_carry_user_id(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """身份只来自登录态：请求体多一个 user_id 就拒绝。"""
    response = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME, "user_id": "someone-else"}
    )

    assert response.status_code == 422


async def test_same_quote_in_two_resumes_points_to_each_document(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """两份简历写了同一句话：各自的事实指向各自的文档，位置按各自的原文计算。"""
    extraction = json.dumps(EXTRACTION, ensure_ascii=False)
    model_outputs.extend([extraction, extraction])
    first = (await signed_in.post("/api/resumes", json={"raw_text": RESUME})).json()
    second = (
        await signed_in.post("/api/resumes", json={"raw_text": "补充说明\n" + RESUME})
    ).json()
    await signed_in.post("/api/facts/batch", json={"action": "confirm_clean"})
    await signed_in.post("/api/facts/commit", json={"base_fact_set_id": None})

    facts = {
        f["source_doc_id"]: f
        for f in (await signed_in.get("/api/facts")).json()["facts"]
    }

    assert facts.keys() == {first["doc_id"], second["doc_id"]}
    for doc_id, fact in facts.items():
        raw = (await signed_in.get(f"/api/resumes/{doc_id}")).json()["raw_text"]
        assert raw[fact["span_start"] : fact["span_end"]] == fact["source_quote"]
    assert facts[second["doc_id"]]["span_start"] > facts[first["doc_id"]]["span_start"]
