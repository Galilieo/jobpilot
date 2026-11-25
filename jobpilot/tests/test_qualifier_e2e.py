"""限定语端到端测试（任务 C §评测 #6）：原文「仅在预发布环境验证」→ 抽取保留 →
假改写写成「已上线」→ 依据检查必须拦下（修订或回退），成品里没有「已上线」。
"""

import asyncio
import json
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    CheckStatus,
    JobPosting,
    ResumeDocument,
    ResumeVersion,
    RunStatus,
    TailoredBullet,
    User,
    UserJob,
)
from jobpilot.services import drafts
from jobpilot.services.facts import extract_facts
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.tailoring_graph import TailorDeps, run_tailoring
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream

Factory = async_sessionmaker[AsyncSession]

QUALIFIER_RESUME = (
    "实习经历\n"
    "云栈物流科技 ｜ 后端开发实习生 ｜ 2025.07-2025.09\n"
    "- 负责重构订单查询接口，仅在预发布环境验证。\n"
)

JOB_SNAPSHOT = {
    "basic_fields": {"title": "后端开发实习生"},
    "requirements": [
        {
            "requirement_id": "11111111-1111-4111-8111-111111111111",
            "type": "hard",
            "text": "有接口开发经验",
        }
    ],
    "duties": [],
    "keywords": ["接口"],
}


async def test_qualifier_preserved_from_extraction_to_tailored_resume(
    session_factory: Factory,
) -> None:
    async with session_factory.begin() as session:
        user = User(email="qualifier@example.invalid", hashed_password="test")
        session.add(user)
        await session.flush()
        user_id: UUID = user.user_id
        doc = ResumeDocument(user_id=user_id, raw_text=QUALIFIER_RESUME)
        posting = JobPosting(
            owner_id=user_id,
            raw_text="招后端实习生",
            content_hash="qualifier" + "0" * 55,
            parsed_json=JOB_SNAPSHOT,
        )
        session.add_all([doc, posting])
        await session.flush()
        doc_id = doc.doc_id
        user_job = UserJob(user_id=user_id, posting_id=posting.posting_id)
        parse_run = AiRun(user_id=user_id, kind="parse_resume")
        session.add_all([user_job, parse_run])
        await session.flush()
        user_job_id, parse_run_id = user_job.user_job_id, parse_run.run_id

    # 1. 抽取阶段：保留限定语「仅在预发布环境验证」
    extract_output = {
        "experiences": [
            {
                "name": "云栈物流科技 后端开发实习",
                "section": "internship",
                "heading_quote": "云栈物流科技 ｜ 后端开发实习生 ｜ 2025.07-2025.09",
                "facts": [
                    {
                        "kind": "action",
                        "responsibility": "own",
                        "responsibility_stated": True,
                        "text": "负责重构订单查询接口（仅在预发布环境验证）",
                        "has_number": False,
                        "source_quote": "负责重构订单查询接口，仅在预发布环境验证。",
                    }
                ],
            }
        ],
        "criteria": {
            "onsite_days_per_week": None,
            "internship_months": None,
            "earliest_start": None,
        },
        "education": [],
    }
    async with fake_upstream(
        lambda _: completion_response(json.dumps(extract_output, ensure_ascii=False))
    ) as client:
        extracted = await extract_facts(
            session_factory,
            user_id,
            parse_run_id,
            client=client,
            budget=ModelBudget(remaining_calls=5, remaining_tokens=50_000),
            doc_id=doc_id,
            raw_text=QUALIFIER_RESUME,
        )

    assert len(extracted.facts) == 1
    assert "仅在预发布环境验证" in extracted.facts[0].text
    assert extracted.facts[0].quote_verified

    # 2. 确认并发布成事实库 v1
    async with session_factory.begin() as session:
        doc_row = await session.get_one(ResumeDocument, doc_id)
        doc_row.sections_json = [s.model_dump(mode="json") for s in extracted.sections]
        [draft_item] = await drafts.add_extracted(session, user_id, extracted.facts)
        await drafts.edit_fact(
            session, user_id, draft_item.lineage_id, drafts.FactChanges(confirmed=True)
        )
        fact_set = await drafts.publish_draft(session, user_id, None)
        [published_fact] = await drafts.facts_of(session, fact_set)
        tailor_run = AiRun(
            user_id=user_id,
            kind="tailor",
            user_job_id=user_job_id,
            fact_set_id=fact_set.fact_set_id,
            base_doc_id=doc_id,
            job_snapshot_json=JOB_SNAPSHOT,
        )
        session.add(tailor_run)
        await session.flush()
        tailor_run_id = tailor_run.run_id

    # 3. 改写流程：假改写写成「已上线」，假依据检查看到限定语后拦下，
    # 修订仍夸大时最终回退成事实原文，成品里绝不出现「已上线」。
    fact_id_str = str(published_fact.fact_id)
    ground_check_saw_qualifier: list[bool] = []

    def tailor_respond(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        user_msg = "\n".join(str(m.get("content", "")) for m in messages)
        if 'label="job_requirements"' in user_msg:
            return completion_response(
                json.dumps(
                    {
                        "items": [
                            {
                                "requirement_id": JOB_SNAPSHOT["requirements"][0][
                                    "requirement_id"
                                ],
                                "verdict": "covered",
                                "fact_ids": [fact_id_str],
                                "reason": "有接口重构经历",
                                "suggestion": "保留验证范围",
                            }
                        ],
                        "main_gaps": [],
                    },
                    ensure_ascii=False,
                )
            )
        if 'label="facts"' in user_msg and 'label="sentence"' in user_msg:
            ground_check_saw_qualifier.append("仅在预发布环境验证" in user_msg)
            return completion_response(
                json.dumps(
                    {
                        "verdict": "unsupported",
                        "unsupported_spans": [
                            {
                                "text": "已上线",
                                "reason": "事实明确写明仅在预发布环境验证",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        if 'label="issues"' in user_msg:
            return completion_response(
                json.dumps(
                    {"text": "负责重构订单查询接口并已上线", "fact_ids": ["f1"]},
                    ensure_ascii=False,
                )
            )
        return completion_response(
            json.dumps(
                {
                    "bullets": [
                        {
                            "text": "负责重构订单查询接口并已上线",
                            "fact_ids": ["f1"],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    async with fake_upstream(tailor_respond) as client:
        deps = TailorDeps(
            session_factory=session_factory,
            client=client,
            budget=ModelBudget(remaining_calls=30, remaining_tokens=100_000),
            limiter=asyncio.Semaphore(4),
        )
        await run_tailoring(deps, user_id, tailor_run_id)

    assert ground_check_saw_qualifier and all(ground_check_saw_qualifier)
    async with session_factory() as session:
        run_row = await session.get_one(AiRun, tailor_run_id)
        assert run_row.status == RunStatus.SUCCEEDED
        [bullet] = (
            await session.scalars(
                select(TailoredBullet).where(TailoredBullet.run_id == tailor_run_id)
            )
        ).all()
        assert bullet.check_status == CheckStatus.FALLBACK
        assert "已上线" not in bullet.text
        assert "仅在预发布环境验证" in bullet.text

        [version] = (
            await session.scalars(
                select(ResumeVersion).where(ResumeVersion.user_id == user_id)
            )
        ).all()
        serialized = json.dumps(version.content_json, ensure_ascii=False)
        assert "已上线" not in serialized
        assert "仅在预发布环境验证" in serialized
