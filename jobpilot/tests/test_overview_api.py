"""工作台汇总接口测试：事实库、不编造记录、岗位看板归类与跨用户隔离。"""

from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    CheckStatus,
    Fact,
    FactDraft,
    FactSet,
    JobPosting,
    JobStage,
    MatchReport,
    ResumeDocument,
    ResumeVersion,
    RunStatus,
    TailoredBullet,
    TailoredResume,
    UserJob,
)
from jobpilot.services.resume_content import ResumeContent

Factory = async_sessionmaker[AsyncSession]


async def _sign_in(client: httpx.AsyncClient, email: str) -> UUID:
    credentials = {"email": email, "password": "overview-test-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success
    return UUID((await client.get("/api/me")).json()["user_id"])


async def _add_job(
    db: AsyncSession,
    user_id: UUID,
    *,
    title: str,
    stage: str = JobStage.SAVED,
    override_title: str | None = None,
) -> UserJob:
    posting = JobPosting(
        owner_id=user_id,
        raw_text=f"岗位 {title}",
        content_hash=uuid4().hex.ljust(64, "0"),
        parsed_json={
            "basic_fields": {"title": title, "company": "示例科技", "location": "上海"},
            "requirements": [],
            "duties": [],
            "keywords": [],
        },
    )
    db.add(posting)
    await db.flush()
    overrides = {"basic_fields": {"title": override_title}} if override_title else {}
    user_job = UserJob(
        user_id=user_id,
        posting_id=posting.posting_id,
        stage=stage,
        field_overrides_json=overrides,
    )
    db.add(user_job)
    await db.flush()
    return user_job


def _make_fact(user_id: UUID, fact_set_id: UUID, text: str) -> Fact:
    return Fact(
        user_id=user_id,
        fact_set_id=fact_set_id,
        lineage_id=uuid4(),
        experience="示例实习",
        section="internship",
        kind="action",
        responsibility="own",
        text=text,
        has_number=False,
        source="user",
    )


async def test_empty_account_returns_zero_overview(
    async_client: httpx.AsyncClient,
) -> None:
    await _sign_in(async_client, "empty@overview.invalid")

    response = await async_client.get("/api/overview")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "facts": {"published_count": 0, "version": None, "pending_count": 0},
        "grounding": {
            "total": 0,
            "passed": 0,
            "fallback": 0,
            "pending": 0,
            "resume_count": 0,
        },
        "board": {"unmatched": [], "matched": [], "tailored": [], "applied": []},
    }


async def test_overview_aggregates_facts_grounding_and_board_and_isolates_users(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    user_id = await _sign_in(async_client, "owner@overview.invalid")

    async with session_factory.begin() as db:
        doc = ResumeDocument(user_id=user_id, raw_text="简历原文")
        v1 = FactSet(user_id=user_id, version=1)
        v2 = FactSet(user_id=user_id, version=2)
        db.add_all([doc, v1, v2])
        await db.flush()

        # v1 有 1 条，v2 有 2 条，应只统计最新已发布版本 v2。
        db.add(_make_fact(user_id, v1.fact_set_id, "旧事实"))
        db.add(_make_fact(user_id, v2.fact_set_id, "负责后端接口"))
        db.add(_make_fact(user_id, v2.fact_set_id, "使用 Redis 缓存"))
        db.add(
            FactDraft(
                user_id=user_id,
                base_fact_set_id=v2.fact_set_id,
                items_json=[
                    {
                        "lineage_id": str(uuid4()),
                        "status": "pending",
                        "experience": "项目",
                        "kind": "action",
                        "responsibility": "own",
                        "text": "待确认条目",
                        "has_number": False,
                        "source": "user",
                    },
                    {
                        "lineage_id": str(uuid4()),
                        "status": "confirmed",
                        "experience": "项目",
                        "kind": "action",
                        "responsibility": "own",
                        "text": "已确认条目",
                        "has_number": False,
                        "source": "user",
                    },
                ],
            )
        )

        unmatched_job = await _add_job(db, user_id, title="未匹配岗位")
        matched_job = await _add_job(db, user_id, title="已匹配岗位")
        tailored_job = await _add_job(
            db, user_id, title="原标题", override_title="改过标题的简历岗"
        )
        applied_job = await _add_job(
            db, user_id, title="已投递岗位", stage=JobStage.INTERVIEWING
        )
        await _add_job(db, user_id, title="归档岗位", stage=JobStage.ARCHIVED)

        # 已匹配岗位（有匹配报告，无定制简历）
        match_run = AiRun(
            user_id=user_id,
            kind="match",
            status=RunStatus.SUCCEEDED,
            user_job_id=matched_job.user_job_id,
            fact_set_id=v2.fact_set_id,
            job_snapshot_json={},
            finished_at=doc.created_at,
        )
        db.add(match_run)
        await db.flush()
        db.add(
            MatchReport(
                user_id=user_id, run_id=match_run.run_id, items_json=[], main_gaps=[]
            )
        )

        # 已生成简历岗位（用旧版 v1 事实库生成，应标为 resume_stale=True）
        tailor_run = AiRun(
            user_id=user_id,
            kind="tailor",
            status=RunStatus.SUCCEEDED,
            user_job_id=tailored_job.user_job_id,
            fact_set_id=v1.fact_set_id,
            base_doc_id=doc.doc_id,
            job_snapshot_json={},
            finished_at=doc.created_at,
        )
        db.add(tailor_run)
        await db.flush()
        statuses = [
            CheckStatus.PASSED,
            CheckStatus.PASSED,
            CheckStatus.FALLBACK,
            CheckStatus.PENDING,
        ]
        for idx, check_status in enumerate(statuses):
            db.add(
                TailoredBullet(
                    run_id=tailor_run.run_id,
                    user_id=user_id,
                    fact_set_id=v1.fact_set_id,
                    section="experience",
                    experience="示例实习",
                    order_no=idx,
                    text=f"条目 {idx}",
                    check_status=check_status,
                )
            )
        resume_id = uuid4()
        version_id = uuid4()
        content = ResumeContent(header=["测试"], sections=[]).model_dump(mode="json")
        db.add(
            TailoredResume(
                resume_id=resume_id,
                run_id=tailor_run.run_id,
                user_id=user_id,
                current_version_id=version_id,
            )
        )
        db.add(
            ResumeVersion(
                resume_version_id=version_id,
                resume_id=resume_id,
                user_id=user_id,
                version=1,
                content_json=content,
            )
        )

    data = (await async_client.get("/api/overview")).json()
    assert data["facts"] == {"published_count": 2, "version": 2, "pending_count": 1}
    assert data["grounding"] == {
        "total": 4,
        "passed": 2,
        "fallback": 1,
        "pending": 1,
        "resume_count": 1,
    }
    board = data["board"]
    assert [c["job_id"] for c in board["unmatched"]] == [str(unmatched_job.user_job_id)]
    assert [c["job_id"] for c in board["matched"]] == [str(matched_job.user_job_id)]
    assert [
        (c["job_id"], c["title"], c["resume_stale"]) for c in board["tailored"]
    ] == [(str(tailored_job.user_job_id), "改过标题的简历岗", True)]
    assert [(c["job_id"], c["stage"]) for c in board["applied"]] == [
        (str(applied_job.user_job_id), "interviewing")
    ]

    # 另一个用户登录后看到的应全部为空，不受第一个用户数据影响。
    await async_client.post("/api/auth/logout")
    await _sign_in(async_client, "other@overview.invalid")
    other = (await async_client.get("/api/overview")).json()
    assert other == {
        "facts": {"published_count": 0, "version": None, "pending_count": 0},
        "grounding": {
            "total": 0,
            "passed": 0,
            "fallback": 0,
            "pending": 0,
            "resume_count": 0,
        },
        "board": {"unmatched": [], "matched": [], "tailored": [], "applied": []},
    }
