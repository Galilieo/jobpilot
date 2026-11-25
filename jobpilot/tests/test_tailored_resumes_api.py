"""定制简历列表接口测试：按更新时间倒序、状态区分、版本号与跨用户隔离。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    CheckStatus,
    FactSet,
    JobPosting,
    ResumeDocument,
    ResumeVersion,
    RunStatus,
    SectionKind,
    TailoredResume,
    UserJob,
)
from jobpilot.services.resume_content import (
    Entry,
    ResumeContent,
    ResumeSection,
    bullet_of,
)

Factory = async_sessionmaker[AsyncSession]


async def _sign_in(client: httpx.AsyncClient, email: str) -> UUID:
    credentials = {"email": email, "password": "tailored-list-1"}
    assert (await client.post("/api/auth/register", json=credentials)).is_success
    assert (await client.post("/api/auth/login", json=credentials)).is_success
    return UUID((await client.get("/api/me")).json()["user_id"])


async def _seed_tailored_resume(
    db: AsyncSession,
    user_id: UUID,
    *,
    doc_id: UUID,
    fact_set_id: UUID,
    job_title: str,
    job_company: str,
    version_number: int,
    created_at: datetime,
    bullet_origin: CheckStatus = CheckStatus.PASSED,
    content: ResumeContent | None = None,
) -> tuple[UUID, UUID]:
    """直接写入一次成功的改写运行和它的简历，返回 (个人岗位 ID, 运行 ID)。

    没给 content 时用一条条目的固定内容，这条条目的生成结果取 bullet_origin。
    """
    if content is None:
        content = ResumeContent(
            header=["测试同学"],
            sections=[
                ResumeSection(
                    title="项目经历",
                    kind=SectionKind.PROJECT,
                    entries=[
                        Entry(
                            entry_id="e1",
                            experience="示例项目",
                            heading="示例项目",
                            title="示例项目",
                            bullets=[
                                bullet_of(
                                    uuid4(),
                                    "完成接口开发",
                                    [uuid4()],
                                    bullet_origin,
                                    [],
                                )
                            ],
                        )
                    ],
                )
            ],
        )
    posting = JobPosting(
        owner_id=user_id,
        raw_text=f"{job_company} {job_title}",
        content_hash=uuid4().hex.ljust(64, "0"),
        parsed_json={
            "basic_fields": {"title": job_title, "company": job_company},
            "requirements": [],
            "duties": [],
            "keywords": [],
        },
    )
    db.add(posting)
    await db.flush()
    user_job = UserJob(user_id=user_id, posting_id=posting.posting_id)
    db.add(user_job)
    await db.flush()

    run = AiRun(
        user_id=user_id,
        kind="tailor",
        status=RunStatus.SUCCEEDED,
        user_job_id=user_job.user_job_id,
        fact_set_id=fact_set_id,
        base_doc_id=doc_id,
        job_snapshot_json=posting.parsed_json,
        finished_at=created_at,
    )
    db.add(run)
    await db.flush()

    resume_id = uuid4()
    version_id = uuid4()
    db.add(
        TailoredResume(
            resume_id=resume_id,
            run_id=run.run_id,
            user_id=user_id,
            current_version_id=version_id,
        )
    )
    db.add(
        ResumeVersion(
            resume_version_id=version_id,
            resume_id=resume_id,
            user_id=user_id,
            version=version_number,
            content_json=content.model_dump(mode="json"),
            created_at=created_at,
        )
    )
    await db.flush()
    return user_job.user_job_id, run.run_id


async def test_tailored_resumes_ordered_with_status_and_isolated(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    user_id = await _sign_in(async_client, "resumes@tailored.invalid")
    assert (await async_client.get("/api/tailored-resumes")).json() == []

    now = datetime.now(UTC)
    async with session_factory.begin() as db:
        doc = ResumeDocument(user_id=user_id, raw_text="简历原文")
        v1 = FactSet(user_id=user_id, version=1)
        v2 = FactSet(user_id=user_id, version=2)
        db.add_all([doc, v1, v2])
        await db.flush()

        job_old, _ = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=v1.fact_set_id,
            job_title="后端实习",
            job_company="示例科技",
            version_number=1,
            created_at=now - timedelta(hours=2),
            bullet_origin=CheckStatus.PASSED,
        )
        job_mid, _ = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=v2.fact_set_id,
            job_title="算法实习",
            job_company="星桥数据",
            version_number=2,
            created_at=now - timedelta(hours=1),
            bullet_origin=CheckStatus.PENDING,
        )
        job_new, _ = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=doc.doc_id,
            fact_set_id=v2.fact_set_id,
            job_title="全栈实习",
            job_company="云栈物流",
            version_number=3,
            created_at=now,
            bullet_origin=CheckStatus.PASSED,
        )

    listed = (await async_client.get("/api/tailored-resumes")).json()
    assert [
        (
            item["job_id"],
            item["job_title"],
            item["job_company"],
            item["fact_set_version"],
            item["latest_fact_set_version"],
            item["current_version"],
            item["status"],
        )
        for item in listed
    ] == [
        (str(job_new), "全栈实习", "云栈物流", 2, 2, 3, "edited"),
        (str(job_mid), "算法实习", "星桥数据", 2, 2, 2, "needs_attention"),
        (str(job_old), "后端实习", "示例科技", 1, 2, 1, "generated"),
    ]

    # 另一个用户看不到第一个用户的任何定制简历。
    await async_client.post("/api/auth/logout")
    await _sign_in(async_client, "other@tailored.invalid")
    assert (await async_client.get("/api/tailored-resumes")).json() == []
