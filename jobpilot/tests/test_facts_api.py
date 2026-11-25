"""简历上传与事实库接口：用假模型走通「提取 → 上传拆分 → 确认 → 发布」。

隔离（A 碰不到 B 的事实和简历）由 test_isolation.py 覆盖。
"""

import json
from uuid import uuid4

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


def new_idempotency_headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


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
        "/api/extract-text",
        files={"file": ("resume.txt", RESUME.encode(), "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": RESUME.rstrip("\n")}
    async with session_factory() as db:
        assert (await db.scalars(select(ResumeDocument))).all() == []


async def test_extract_sends_images_to_ocr(signed_in: httpx.AsyncClient) -> None:
    response = await signed_in.post(
        "/api/extract-text",
        files={"file": ("resume.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )

    assert response.status_code == 422
    assert "/api/ocr" in response.json()["detail"]


# ---------- 上传、确认、发布 ----------


@pytest.mark.parametrize(
    "changes", [{"confirmed": True}, {"confirmed": False}, {"responsibility": "lead"}]
)
async def test_review_requires_displayed_revision(
    signed_in: httpx.AsyncClient, model_outputs: list[str], changes: dict[str, object]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    assert uploaded.status_code == 201
    clean, _ = uploaded.json()["items"]

    response = await signed_in.put(f"/api/facts/{clean['lineage_id']}", json=changes)

    assert response.status_code == 422
    detail = await signed_in.get(f"/api/facts/{clean['lineage_id']}")
    assert detail.json()["draft"]["status"] == "pending"
    assert detail.json()["draft"]["responsibility"] == "own"
    assert detail.json()["revision"] == uploaded.json()["revision"]


async def test_stale_confirmation_does_not_confirm_edited_text(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    _, item = uploaded.json()["items"]
    path = f"/api/facts/{item['lineage_id']}"
    displayed = (await signed_in.get(path)).json()
    assert "30%" in displayed["draft"]["text"]

    revised_text = "优化查询后响应时间降低 20%"
    edited = await signed_in.put(
        path, json={"text": revised_text, "expected_revision": displayed["revision"]}
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == displayed["revision"] + 1

    stale = await signed_in.put(
        path, json={"confirmed": True, "expected_revision": displayed["revision"]}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["revision"] == edited.json()["revision"]
    current = (await signed_in.get(path)).json()
    assert current["draft"]["text"] == revised_text
    assert current["draft"]["status"] == "pending"
    assert current["revision"] == edited.json()["revision"]

    confirmed = await signed_in.put(
        path, json={"confirmed": True, "expected_revision": current["revision"]}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["text"] == revised_text
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["revision"] == current["revision"] + 1


async def test_stale_batch_does_not_confirm_new_resume_items(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    experience = EXTRACTION["experiences"][0]
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    displayed = (await signed_in.get("/api/facts")).json()["draft"]

    quotes = [f"负责实现第 {week} 周课程筛选" for week in range(1, 6)]
    facts = [
        {
            **experience["facts"][0],
            "text": quote,
            "source_quote": quote,
            "has_number": True,
        }
        for quote in quotes
    ]
    model_outputs.append(
        json.dumps(
            {"experiences": [{**experience, "facts": facts}]}, ensure_ascii=False
        )
    )
    raw_text = "\n".join([experience["heading_quote"], *quotes])
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": raw_text}, headers=new_idempotency_headers()
    )
    assert uploaded.status_code == 201
    assert len(uploaded.json()["items"]) == 5
    assert uploaded.json()["revision"] == displayed["revision"] + 1

    stale = await signed_in.post(
        "/api/facts/batch",
        json={"action": "confirm_clean", "expected_revision": displayed["revision"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["revision"] == uploaded.json()["revision"]
    current = (await signed_in.get("/api/facts")).json()["draft"]
    assert current["revision"] == uploaded.json()["revision"]
    assert current["items"] == [*displayed["items"], *uploaded.json()["items"]]
    assert {item["status"] for item in current["items"]} == {"pending"}


async def test_stale_confirmation_of_removed_item_reports_conflict(
    signed_in: httpx.AsyncClient,
) -> None:
    added = await signed_in.post(
        "/api/facts",
        json={
            "experience": "课程排班面板",
            "kind": "action",
            "responsibility": "own",
            "text": "实现按周次筛选课程的功能",
        },
    )
    assert added.status_code == 201
    item = added.json()
    path = f"/api/facts/{item['lineage_id']}"
    deleted = await signed_in.delete(f"{path}?expected_revision={item['revision']}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    revision = int(deleted.headers["X-Draft-Revision"])
    assert revision == item["revision"] + 1

    stale = await signed_in.put(
        path, json={"confirmed": True, "expected_revision": item["revision"]}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["revision"] == revision
    missing = await signed_in.put(
        path, json={"confirmed": True, "expected_revision": revision}
    )
    assert missing.status_code == 404


async def test_upload_confirm_and_publish(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))

    uploaded = await signed_in.post(
        "/api/resumes",
        json={"filename": "resume.txt", "raw_text": RESUME},
        headers=new_idempotency_headers(),
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["revision"] == 1
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

    missing = await signed_in.post("/api/facts/batch", json={"action": "confirm_clean"})
    assert missing.status_code == 422
    confirmed = await signed_in.post(
        "/api/facts/batch",
        json={"action": "confirm_clean", "expected_revision": body["revision"]},
    )
    assert confirmed.json() == {"changed": 1, "revision": 2}

    missing = await signed_in.post("/api/facts/commit", json={"base_fact_set_id": None})
    assert missing.status_code == 422
    stale_revision = await signed_in.post(
        "/api/facts/commit",
        json={"base_fact_set_id": None, "expected_revision": body["revision"]},
    )
    assert stale_revision.status_code == 409
    assert stale_revision.json()["detail"]["revision"] == confirmed.json()["revision"]
    assert (await signed_in.get("/api/facts")).json()["fact_set"] is None

    published = await signed_in.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": None,
            "expected_revision": confirmed.json()["revision"],
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 1
    assert published.json()["revision"] == 3

    library = (await signed_in.get("/api/facts")).json()
    assert library["draft"]["revision"] == published.json()["revision"]
    assert [f["lineage_id"] for f in library["facts"]] == [clean["lineage_id"]]
    assert [i["lineage_id"] for i in library["draft"]["items"]] == [
        flagged["lineage_id"]
    ]

    # 还拿着旧基准（空）再发布：版本已变，409。
    stale = await signed_in.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": None,
            "expected_revision": library["draft"]["revision"],
        },
    )
    assert stale.status_code == 409
    # 基准正确但没有改动：400。
    nothing = await signed_in.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": published.json()["fact_set_id"],
            "expected_revision": library["draft"]["revision"],
        },
    )
    assert nothing.status_code == 400


async def test_set_responsibility_then_confirm_clean(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """回答「这段经历里没写职责的是你负责的吗」，之后一键确认就能覆盖它们。"""
    previous = (await signed_in.get("/api/facts")).json()["draft"]["revision"]
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    before = (await signed_in.get("/api/facts")).json()["draft"]
    responsibility = {
        "action": "set_responsibility",
        "experience": "课程排班面板",
        "responsibility": "own",
    }
    missing = await signed_in.post("/api/facts/batch", json=responsibility)
    assert missing.status_code == 422
    stale = await signed_in.post(
        "/api/facts/batch",
        json={**responsibility, "expected_revision": previous},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["revision"] == before["revision"]
    assert (await signed_in.get("/api/facts")).json()["draft"] == before
    answered = await signed_in.post(
        "/api/facts/batch",
        json={**responsibility, "expected_revision": before["revision"]},
    )
    confirmed = await signed_in.post(
        "/api/facts/batch",
        json={
            "action": "confirm_clean",
            "expected_revision": answered.json()["revision"],
        },
    )

    assert answered.json() == {"changed": 1, "revision": 2}
    assert confirmed.json() == {"changed": 2, "revision": 3}


async def test_quote_failure_blocks_confirmation(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    bad = json.loads(json.dumps(EXTRACTION))
    bad["experiences"][0]["facts"][0]["source_quote"] = "原文里没有这一句"
    model_outputs.append(json.dumps(bad, ensure_ascii=False))
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    blocked, _ = uploaded.json()["items"]

    response = await signed_in.put(
        f"/api/facts/{blocked['lineage_id']}",
        json={"confirmed": True, "expected_revision": uploaded.json()["revision"]},
    )

    assert response.status_code == 422
    assert "修改文字或删除" in response.json()["detail"]
    detail = await signed_in.get(f"/api/facts/{blocked['lineage_id']}")
    assert detail.json()["revision"] == uploaded.json()["revision"]


async def test_model_failure_marks_run_failed_and_keeps_resume(
    signed_in: httpx.AsyncClient,
    model_outputs: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    model_outputs.append("不是 JSON")
    model_outputs.append("还不是 JSON")  # JSON 修复重发也失败

    response = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )

    assert response.status_code == 502
    async with session_factory() as db:
        [run] = (await db.scalars(select(AiRun))).all()
        assert run.status == RunStatus.FAILED
        assert len((await db.scalars(select(ResumeDocument))).all()) == 1
    library = (await signed_in.get("/api/facts")).json()
    assert library["draft"]["items"] == []
    assert library["draft"]["revision"] == 0


async def test_requests_cannot_carry_user_id(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """身份只来自登录态：请求体多一个 user_id 就拒绝。"""
    response = await signed_in.post(
        "/api/resumes",
        json={"raw_text": RESUME, "user_id": "someone-else"},
        headers=new_idempotency_headers(),
    )

    assert response.status_code == 422


async def test_same_quote_in_two_resumes_points_to_each_document(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """两份简历写了同一句话：各自的事实指向各自的文档，位置按各自的原文计算。"""
    extraction = json.dumps(EXTRACTION, ensure_ascii=False)
    model_outputs.extend([extraction, extraction])
    first = (
        await signed_in.post(
            "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
        )
    ).json()
    second = (
        await signed_in.post(
            "/api/resumes",
            json={"raw_text": "补充说明\n" + RESUME},
            headers=new_idempotency_headers(),
        )
    ).json()
    confirmed = await signed_in.post(
        "/api/facts/batch",
        json={"action": "confirm_clean", "expected_revision": second["revision"]},
    )
    await signed_in.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": None,
            "expected_revision": confirmed.json()["revision"],
        },
    )

    facts = {
        f["source_doc_id"]: f
        for f in (await signed_in.get("/api/facts")).json()["facts"]
    }

    assert facts.keys() == {first["doc_id"], second["doc_id"]}
    for doc_id, fact in facts.items():
        raw = (await signed_in.get(f"/api/resumes/{doc_id}")).json()["raw_text"]
        assert raw[fact["span_start"] : fact["span_end"]] == fact["source_quote"]
    assert facts[second["doc_id"]]["span_start"] > facts[first["doc_id"]]["span_start"]


async def test_wrong_experience_requires_individual_confirmation(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    quote = "负责数据库迁移"
    raw = f"甲公司实习\n{quote}\n乙公司实习\n维护前端页面"
    model_outputs.append(
        json.dumps(
            {
                "experiences": [
                    {
                        "name": "乙公司实习",
                        "section": "internship",
                        "heading_quote": "乙公司后端研发实习",
                        "facts": [
                            {
                                "kind": "action",
                                "responsibility": "own",
                                "responsibility_stated": True,
                                "text": quote,
                                "has_number": False,
                                "source_quote": quote,
                            }
                        ],
                    }
                ]
            }
        )
    )
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": raw}, headers=new_idempotency_headers()
    )
    [item] = uploaded.json()["items"]
    assert item["experience"] == "乙公司实习"
    assert (
        item["quote_verified"] and raw[item["span_start"] : item["span_end"]] == quote
    )
    assert item["review_reasons"] == ["experience_uncertain"]
    batch = await signed_in.post(
        "/api/facts/batch",
        json={
            "action": "confirm_clean",
            "expected_revision": uploaded.json()["revision"],
        },
    )
    assert batch.json() == {"changed": 0, "revision": 2}
    edited = await signed_in.put(
        f"/api/facts/{item['lineage_id']}",
        json={"confirmed": True, "expected_revision": batch.json()["revision"]},
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "confirmed"


@pytest.mark.parametrize("changes", [{"text": "改了文字"}, {"kind": "technology"}])
async def test_text_and_kind_edits_require_expected_revision(
    signed_in: httpx.AsyncClient, model_outputs: list[str], changes: dict[str, object]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    clean, _ = uploaded.json()["items"]
    path = f"/api/facts/{clean['lineage_id']}"
    rev = uploaded.json()["revision"]

    missing = await signed_in.put(path, json=changes)
    assert missing.status_code == 422

    # 另一窗口先推进修订号，再用旧修订号改文字/类型应 409。
    await signed_in.put(path, json={"confirmed": True, "expected_revision": rev})
    stale = await signed_in.put(path, json={**changes, "expected_revision": rev})
    assert stale.status_code == 409
    assert stale.json()["detail"]["revision"] == rev + 1


async def test_delete_requires_current_revision_and_blocks_stale_window(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    model_outputs.append(json.dumps(EXTRACTION, ensure_ascii=False))
    uploaded = await signed_in.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=new_idempotency_headers()
    )
    clean, _ = uploaded.json()["items"]
    path = f"/api/facts/{clean['lineage_id']}"
    stale_rev = uploaded.json()["revision"]

    missing = await signed_in.delete(path)
    assert missing.status_code == 422

    # 窗口 A 先确认了这条草稿事实；窗口 B 拿着旧修订号删除，
    # 必须返回 409 且保留已确认状态。
    confirmed = await signed_in.put(
        path, json={"confirmed": True, "expected_revision": stale_rev}
    )
    assert confirmed.status_code == 200
    new_rev = confirmed.json()["revision"]

    stale_delete = await signed_in.delete(f"{path}?expected_revision={stale_rev}")
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["revision"] == new_rev

    detail = (await signed_in.get(path)).json()
    assert detail["draft"]["status"] == "confirmed"
    assert detail["revision"] == new_rev

    # 用最新修订号删除则成功。
    ok_delete = await signed_in.delete(f"{path}?expected_revision={new_rev}")
    assert ok_delete.status_code == 204
    assert int(ok_delete.headers["X-Draft-Revision"]) == new_rev + 1


async def test_criteria_and_education_routes_and_revision_conflicts(
    signed_in: httpx.AsyncClient,
) -> None:
    """静态路由不被 {lineage_id} 当成 UUID 拦成 422；增删改查、发布和 409 正常。"""
    lib0 = (await signed_in.get("/api/facts")).json()
    rev = lib0["draft"]["revision"]

    # 1. 求职条件：新增并确认、过期修订号 409
    crit_res = await signed_in.put(
        "/api/facts/criteria",
        json={
            "field": "onsite_days_per_week",
            "int_value": 5,
            "confirmed": True,
            "expected_revision": rev,
        },
    )
    assert crit_res.status_code == 200, crit_res.text
    assert crit_res.json()["criterion"]["int_value"] == 5
    assert crit_res.json()["criterion"]["status"] == "confirmed"
    rev = crit_res.json()["revision"]

    stale_crit = await signed_in.put(
        "/api/facts/criteria",
        json={
            "field": "onsite_days_per_week",
            "int_value": 3,
            "expected_revision": rev - 1,
        },
    )
    assert stale_crit.status_code == 409

    # 2. 教育：新增、修改、发布、只改条件或教育也能发布
    edu_res = await signed_in.post(
        "/api/facts/education",
        json={
            "school": "东澜理工学院",
            "major": "软件工程",
            "degree": "bachelor",
            "status": "enrolled",
            "start_text": "2023.09",
            "end_text": "2027.06（预计）",
            "expected_revision": rev,
        },
    )
    assert edu_res.status_code == 201, edu_res.text
    edu_id = edu_res.json()["education_id"]
    rev = edu_res.json()["revision"]

    edited_edu = await signed_in.put(
        f"/api/facts/education/{edu_id}",
        json={"end_text": "2027.06", "expected_revision": rev},
    )
    assert edited_edu.status_code == 200, edited_edu.text
    rev = edited_edu.json()["revision"]

    published = await signed_in.post(
        "/api/facts/commit",
        json={"base_fact_set_id": None, "expected_revision": rev},
    )
    assert published.status_code == 200, published.text
    rev = published.json()["revision"]

    lib1 = (await signed_in.get("/api/facts")).json()
    assert len(lib1["criteria"]) == 1 and lib1["criteria"][0]["int_value"] == 5
    assert len(lib1["education"]) == 1 and lib1["education"][0]["end_text"] == "2027.06"

    # 3. 删除已发布教育需要正确修订号，发布后从新版本移除
    stale_del = await signed_in.delete(
        f"/api/facts/education/{edu_id}?expected_revision={rev - 1}"
    )
    assert stale_del.status_code == 409
    deleted = await signed_in.delete(
        f"/api/facts/education/{edu_id}?expected_revision={rev}"
    )
    assert deleted.status_code == 204
    rev = int(deleted.headers["X-Draft-Revision"])
    published2 = await signed_in.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": lib1["fact_set"]["fact_set_id"],
            "expected_revision": rev,
        },
    )
    assert published2.status_code == 200
    lib2 = (await signed_in.get("/api/facts")).json()
    assert lib2["education"] == []
    assert len(lib2["criteria"]) == 1


async def test_two_resumes_same_criteria_quote_point_to_each_doc(
    signed_in: httpx.AsyncClient, model_outputs: list[str]
) -> None:
    """两份简历有相同到岗片段时，各自指向对应的 source_doc_id。"""
    resume1 = "每周可到岗 4 天\n" + RESUME
    resume2 = "补充说明\n每周可到岗 4 天\n" + RESUME
    payload = {
        **EXTRACTION,
        "criteria": {
            "onsite_days_per_week": {"value": 4, "source_quote": "每周可到岗 4 天"},
            "internship_months": None,
            "earliest_start": None,
        },
        "education": [],
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    model_outputs.extend([encoded, encoded])

    up1 = await signed_in.post(
        "/api/resumes", json={"raw_text": resume1}, headers=new_idempotency_headers()
    )
    assert up1.status_code == 201
    c1 = up1.json()["criteria"][0]
    assert c1["source_doc_id"] == up1.json()["doc_id"]
    assert c1["span_start"] == 0

    up2 = await signed_in.post(
        "/api/resumes", json={"raw_text": resume2}, headers=new_idempotency_headers()
    )
    assert up2.status_code == 201
    c2 = up2.json()["criteria"][0]
    assert c2["source_doc_id"] == up2.json()["doc_id"]
    assert c2["span_start"] > 0
