"""只读使用统计服务与脚本测试：核对全部口径、账号隔离、只读事务与不泄密。"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    Application,
    ApplicationStageEvent,
    CallOutcome,
    CheckStatus,
    Conversation,
    Fact,
    FactSet,
    JobPosting,
    Message,
    MessageRole,
    ModelCall,
    ResumeDocument,
    ResumeVersion,
    RunStatus,
    TailoredBullet,
    TailoredResume,
    User,
    UserDecision,
    UserJob,
)
from jobpilot.db.session import read_snapshot
from jobpilot.scripts.usage_stats import parse_args
from jobpilot.services.resume_content import (
    Bullet,
    BulletCheck,
    CheckLayers,
    Entry,
    ResumeContent,
    ResumeSection,
    UserBulletChoice,
)
from jobpilot.services.usage_stats import (
    collect_usage_stats,
    format_usage_stats,
    stats_to_dict,
)

Factory = async_sessionmaker[AsyncSession]

_SECRET_RESUME = "保密简历原文-林某-13800138000"
_SECRET_JD = "保密岗位要求-星辰公司内部JD"
_SECRET_BULLET = "保密改写正文-负责核心网关"
_SECRET_MESSAGE = "保密对话消息正文"


def _make_content(bullets: list[Bullet]) -> dict[str, object]:
    return ResumeContent(
        header=["合成测试候选人"],
        sections=[
            ResumeSection(
                title="实习经历",
                kind="internship",
                entries=[
                    Entry(
                        entry_id="exp-1",
                        experience="示例实习",
                        heading="示例实习",
                        title="示例实习",
                        bullets=bullets,
                    )
                ],
            )
        ],
    ).model_dump(mode="json")


def _make_bullet(
    bullet_id: UUID,
    text_value: str,
    *,
    origin: CheckStatus = CheckStatus.PASSED,
    user_choice: UserBulletChoice | None = None,
) -> Bullet:
    return Bullet(
        bullet_id=bullet_id,
        text=text_value,
        fact_ids=[],
        origin=origin,
        user_choice=user_choice,
        check=BulletCheck(text=text_value, layers=CheckLayers.FULL, issues=[]),
    )


async def _add_user(db: AsyncSession, email: str) -> User:
    user = User(email=email, hashed_password="fake-hash-for-stats-test")
    db.add(user)
    await db.flush()
    return user


async def _add_job(db: AsyncSession, user_id: UUID, marker: str) -> UserJob:
    posting = JobPosting(
        owner_id=user_id,
        raw_text=f"{_SECRET_JD}-{marker}",
        content_hash=uuid4().hex.ljust(64, "0"),
        parsed_json={
            "basic_fields": {},
            "requirements": [],
            "duties": [],
            "keywords": [],
        },
    )
    db.add(posting)
    await db.flush()
    job = UserJob(user_id=user_id, posting_id=posting.posting_id)
    db.add(job)
    await db.flush()
    return job


async def _add_tailored_resume(
    db: AsyncSession,
    user_id: UUID,
    fact_set_id: UUID,
    doc_id: UUID,
    job_id: UUID | None,
    *,
    v1_bullets: list[Bullet],
    cur_bullets: list[Bullet] | None = None,
    check_statuses: list[tuple[CheckStatus, UserDecision | None]] | None = None,
) -> AiRun:
    ts = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
    run = AiRun(
        user_id=user_id,
        kind="tailor",
        status=RunStatus.SUCCEEDED,
        user_job_id=job_id,
        fact_set_id=fact_set_id,
        base_doc_id=doc_id,
        job_snapshot_json={},
        started_at=ts,
        finished_at=ts,
    )
    db.add(run)
    await db.flush()

    for idx, (status, decision) in enumerate(check_statuses or []):
        db.add(
            TailoredBullet(
                run_id=run.run_id,
                user_id=user_id,
                fact_set_id=fact_set_id,
                section="experience",
                experience="示例实习",
                order_no=idx,
                text=f"{_SECRET_BULLET}-{idx}",
                check_status=status,
                user_decision=decision,
            )
        )

    resume_id = uuid4()
    v1_id = uuid4()
    cur_id = v1_id if cur_bullets is None else uuid4()
    db.add(
        TailoredResume(
            resume_id=resume_id,
            run_id=run.run_id,
            user_id=user_id,
            current_version_id=cur_id,
        )
    )
    db.add(
        ResumeVersion(
            resume_version_id=v1_id,
            resume_id=resume_id,
            user_id=user_id,
            version=1,
            content_json=_make_content(v1_bullets),
        )
    )
    if cur_bullets is not None:
        db.add(
            ResumeVersion(
                resume_version_id=cur_id,
                resume_id=resume_id,
                user_id=user_id,
                version=2,
                content_json=_make_content(cur_bullets),
            )
        )
    return run


async def _add_conv_messages(
    db: AsyncSession,
    user_id: UUID,
    conv: Conversation,
    steps_per_turn: list[list[dict[str, object]]],
) -> None:
    run = AiRun(
        user_id=user_id,
        kind="agent",
        status=RunStatus.SUCCEEDED,
        conversation_id=conv.conversation_id,
        finished_at=datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC),
    )
    db.add(run)
    await db.flush()
    db.add(
        Message(
            user_id=user_id,
            conversation_id=conv.conversation_id,
            run_id=run.run_id,
            role=MessageRole.USER,
            event_key="user:1",
            content=_SECRET_MESSAGE,
        )
    )
    for idx, steps in enumerate(steps_per_turn, 1):
        db.add(
            Message(
                user_id=user_id,
                conversation_id=conv.conversation_id,
                run_id=run.run_id,
                role=MessageRole.TOOL,
                event_key=f"tool:{idx}",
                content="",
                steps_json=steps,
            )
        )


async def test_collect_usage_stats_aggregates_isolates_and_protects_privacy(
    session_factory: Factory,
) -> None:
    async with session_factory.begin() as db:
        owner = await _add_user(db, "Stats.Owner@example.com")
        other = await _add_user(db, "other@example.com")

        doc = ResumeDocument(user_id=owner.user_id, raw_text=_SECRET_RESUME)
        fact_set = FactSet(user_id=owner.user_id, version=1)
        db.add_all([doc, fact_set])
        await db.flush()
        db.add(
            Fact(
                user_id=owner.user_id,
                fact_set_id=fact_set.fact_set_id,
                lineage_id=uuid4(),
                experience="示例实习",
                section="internship",
                kind="action",
                responsibility="own",
                text="负责开发接口",
                has_number=False,
                source="user",
            )
        )

        job1 = await _add_job(db, owner.user_id, "j1")
        job2 = await _add_job(db, owner.user_id, "j2")
        job3 = await _add_job(db, owner.user_id, "j3")

        b_edit, b_bold, b_facts, b_del = uuid4(), uuid4(), uuid4(), uuid4()
        run1 = await _add_tailored_resume(
            db,
            owner.user_id,
            fact_set.fact_set_id,
            doc.doc_id,
            job1.user_job_id,
            v1_bullets=[
                _make_bullet(b_edit, "原句一"),
                _make_bullet(b_bold, "加粗不变"),
                _make_bullet(b_facts, "原句三"),
                _make_bullet(b_del, "被删除条目"),
            ],
            cur_bullets=[
                _make_bullet(b_edit, "手动改后的条目"),
                _make_bullet(b_bold, "**加粗**不变"),
                _make_bullet(
                    b_facts,
                    "事实原文",
                    origin=CheckStatus.FALLBACK,
                    user_choice=UserBulletChoice.USE_FACTS,
                ),
            ],
            check_statuses=[
                (CheckStatus.PASSED, None),
                (CheckStatus.PASSED, None),
                (CheckStatus.FALLBACK, UserDecision.REJECTED),
                (CheckStatus.PENDING, None),
            ],
        )
        # 同一岗位在同对话内重新生成一次：完成流程数仍按 1 计，对话改写次数计 2。
        run1_regen = await _add_tailored_resume(
            db,
            owner.user_id,
            fact_set.fact_set_id,
            doc.doc_id,
            job1.user_job_id,
            v1_bullets=[_make_bullet(uuid4(), "未改动的条目")],
            cur_bullets=None,
            check_statuses=[(CheckStatus.PASSED, None)],
        )
        # 无 parse_jd 直接 tailor：不计入完成流程，计入对话改写次数。
        run3 = await _add_tailored_resume(
            db,
            owner.user_id,
            fact_set.fact_set_id,
            doc.doc_id,
            job3.user_job_id,
            v1_bullets=[_make_bullet(uuid4(), "条目")],
            cur_bullets=None,
        )

        # 对话 1：parse_jd(job1) 后改写两次，再 parse_jd(job2) 后改写失败。
        c1 = Conversation(
            user_id=owner.user_id, title="对话1", current_job_id=job1.user_job_id
        )
        # 对话 2：直接 tailor(run3)（无 parse_jd）
        c2 = Conversation(
            user_id=owner.user_id, title="对话2", current_job_id=job3.user_job_id
        )
        # 对话 3：parse_jd(已删岗位) 后改写（AiRun 已删）兜底仍计 1 次。
        c3 = Conversation(user_id=owner.user_id, title="对话3")
        db.add_all([c1, c2, c3])
        await db.flush()

        deleted_job_id = uuid4()
        deleted_run_id = uuid4()
        await _add_conv_messages(
            db,
            owner.user_id,
            c1,
            [
                [
                    {
                        "tool": "parse_jd",
                        "status": "succeeded",
                        "job_id": str(job1.user_job_id),
                    }
                ],
                [
                    {
                        "tool": "tailor_resume",
                        "status": "succeeded",
                        "run_id": str(run1.run_id),
                    }
                ],
                [
                    {
                        "tool": "tailor_resume",
                        "status": "succeeded",
                        "run_id": str(run1_regen.run_id),
                    }
                ],
                [
                    {
                        "tool": "parse_jd",
                        "status": "succeeded",
                        "job_id": str(job2.user_job_id),
                    }
                ],
                [{"tool": "tailor_resume", "status": "failed", "run_id": str(uuid4())}],
            ],
        )
        await _add_conv_messages(
            db,
            owner.user_id,
            c2,
            [
                [
                    {
                        "tool": "tailor_resume",
                        "status": "succeeded",
                        "run_id": str(run3.run_id),
                    }
                ]
            ],
        )
        await _add_conv_messages(
            db,
            owner.user_id,
            c3,
            [
                [
                    {
                        "tool": "parse_jd",
                        "status": "succeeded",
                        "job_id": str(deleted_job_id),
                    }
                ],
                [
                    {
                        "tool": "tailor_resume",
                        "status": "succeeded",
                        "run_id": str(deleted_run_id),
                    }
                ],
            ],
        )

        for stage in (
            "applied",
            "written_test",
            "interviewing",
            "interviewing",
            "offer",
        ):
            app_row = Application(
                user_id=owner.user_id,
                user_job_id=job1.user_job_id,
                job_snapshot_json={},
                stage=stage,
                backfilled=True,
            )
            db.add(app_row)
            await db.flush()
            db.add(
                ApplicationStageEvent(
                    user_id=owner.user_id,
                    application_id=app_row.application_id,
                    from_stage=None,
                    to_stage=stage,
                )
            )

        db.add_all(
            [
                ModelCall(
                    user_id=owner.user_id,
                    purpose="agent",
                    model="deepseek-flash",
                    outcome=CallOutcome.SUCCESS,
                    latency_ms=100,
                    prompt_tokens=1200,
                    completion_tokens=300,
                ),
                ModelCall(
                    user_id=owner.user_id,
                    purpose="rewrite",
                    model="deepseek-flash",
                    outcome=CallOutcome.SUCCESS,
                    latency_ms=80,
                    prompt_tokens=800,
                    completion_tokens=200,
                ),
                ModelCall(
                    user_id=owner.user_id,
                    purpose="match",
                    model="deepseek-flash",
                    outcome=CallOutcome.UNKNOWN,
                    latency_ms=50,
                    prompt_tokens=None,
                    completion_tokens=None,
                ),
            ]
        )

        # 对照账号 B 的全部数据都不应计入 owner。
        other_doc = ResumeDocument(user_id=other.user_id, raw_text="他人简历")
        other_fs = FactSet(user_id=other.user_id, version=1)
        db.add_all([other_doc, other_fs])
        await db.flush()
        other_job = await _add_job(db, other.user_id, "other")
        other_run = await _add_tailored_resume(
            db,
            other.user_id,
            other_fs.fact_set_id,
            other_doc.doc_id,
            other_job.user_job_id,
            v1_bullets=[_make_bullet(uuid4(), "他人条目")],
            check_statuses=[(CheckStatus.PASSED, None)],
        )
        other_conv = Conversation(user_id=other.user_id, title="他人对话")
        db.add(other_conv)
        await db.flush()
        await _add_conv_messages(
            db,
            other.user_id,
            other_conv,
            [
                [
                    {
                        "tool": "parse_jd",
                        "status": "succeeded",
                        "job_id": str(other_job.user_job_id),
                    }
                ],
                [
                    {
                        "tool": "tailor_resume",
                        "status": "succeeded",
                        "run_id": str(other_run.run_id),
                    }
                ],
            ],
        )
        db.add(
            Application(
                user_id=other.user_id,
                user_job_id=other_job.user_job_id,
                job_snapshot_json={},
                stage="rejected",
                backfilled=True,
            )
        )
        db.add(
            ModelCall(
                user_id=other.user_id,
                purpose="agent",
                model="deepseek-flash",
                outcome=CallOutcome.SUCCESS,
                prompt_tokens=9999,
                completion_tokens=9999,
            )
        )

    stats = await collect_usage_stats(session_factory, email="stats.owner@example.com")
    assert stats is not None
    assert stats_to_dict(stats) == {
        "conversations": {
            "total": 3,
            "completed_jd_to_resume_flows": 2,
            "dialogue_tailor_runs": 4,
        },
        "tailored_resumes": {
            "resume_count": 3,
            "total_bullets": 5,
            "passed": 3,
            "fallback": 1,
            "pending": 1,
            "rejected": 1,
        },
        "manual_edits": {
            "resumes_with_edits": 1,
            "edited_bullets": 1,
            "use_facts_bullets": 1,
            "removed_bullets": 1,
        },
        "applications": {
            "total": 5,
            "by_stage": {
                "applied": 1,
                "written_test": 1,
                "interviewing": 2,
                "offer": 1,
                "rejected": 0,
            },
        },
        "model_usage": {
            "total_calls": 3,
            "succeeded_calls": 2,
            "unknown_usage_calls": 1,
            "prompt_tokens": 2000,
            "completion_tokens": 500,
            "total_tokens": 2500,
        },
    }

    by_id = await collect_usage_stats(session_factory, user_id=owner.user_id)
    assert by_id == stats
    assert (
        await collect_usage_stats(session_factory, email="missing@example.com") is None
    )

    rendered = format_usage_stats(stats)
    serialized = json.dumps(stats_to_dict(stats), ensure_ascii=False)
    for forbidden in (
        "Stats.Owner@example.com",
        "stats.owner@example.com",
        str(owner.user_id),
        _SECRET_RESUME,
        _SECRET_JD,
        _SECRET_BULLET,
        _SECRET_MESSAGE,
        "手动改后的条目",
    ):
        assert forbidden not in rendered
        assert forbidden not in serialized

    # 只读保证：在 read_snapshot 里执行写入会被 PostgreSQL 拒绝，统计前后行数也不变。
    with pytest.raises(SQLAlchemyError):
        async with read_snapshot(session_factory) as session:
            await session.execute(
                text("DELETE FROM user_account WHERE user_id = :uid"),
                {"uid": owner.user_id},
            )
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count(Conversation.conversation_id))) == 4
        )
        assert await session.scalar(select(func.count(Application.application_id))) == 6


def test_cli_parse_args() -> None:
    email_args = parse_args(["--email", "user@example.com", "--json"])
    assert email_args.email == "user@example.com"
    assert email_args.user_id is None
    assert email_args.json is True

    uid = uuid4()
    id_args = parse_args(["--user-id", str(uid)])
    assert id_args.user_id == uid
    assert id_args.email is None
    assert id_args.json is False
