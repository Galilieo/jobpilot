"""06 T2.5 验收：从改写到导出走一遍（假模型、真实数据库）。

底稿写「耗时降低 30%」，用户已把事实纠正为 20%。模型坚持写 30%，两次修订仍不改：
组装结果和导出只能有 20%；教育等没改写的部分与底稿相同；
再上传一份简历不改变旧版本的导出；finalize 重复执行也只有一份简历、一个初始版本。
"""

import asyncio
import json
import re
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    Fact,
    FactSet,
    JobPosting,
    ResumeDocument,
    ResumeVersion,
    TailoredResume,
    User,
    UserJob,
)
from jobpilot.db.runs import RunAlreadyFinishedError
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.resume_export import RenderMode, render_html, to_markdown
from jobpilot.services.resume_versions import load_resume
from jobpilot.services.tailoring_graph import TailorDeps, run_tailoring
from jobpilot.services.tailoring_store import load_inputs, save_outcome
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream

Factory = async_sessionmaker[AsyncSession]

INTERN = "云栈物流科技 后端开发实习"
SCHOOL = "澄湖大学"
INTERN_HEADING = "云栈物流科技 | 后端开发实习生 | 2025.07–2025.09"
SCHOOL_HEADING = "澄湖大学 | 计算机科学与技术 | 本科 | 2022.09–2026.06"
RAW = f"""李明
上海 | liming@example.invalid

实习经历
{INTERN_HEADING}
负责运单查询接口，P95 耗时降低 30%。

教育经历
{SCHOOL_HEADING}
主修操作系统、编译原理
"""
OLD_SENTENCE = "P95 耗时降低 30%"
CORRECTED = "运单查询 P95 耗时降低 20%"
REQUIREMENT = "33333333-3333-4333-8333-333333333333"
SNAPSHOT = {
    "basic_fields": {"title": "后端开发实习生"},
    "requirements": [
        {"requirement_id": REQUIREMENT, "type": "hard", "text": "有性能优化经验"}
    ],
    "duties": [],
    "keywords": [],
}


def _resume_fact(user_id, fact_set_id, doc_id, experience, section, text):
    start = RAW.index(text)
    return Fact(
        user_id=user_id,
        fact_set_id=fact_set_id,
        lineage_id=uuid4(),
        experience=experience,
        section=section,
        kind="action",
        responsibility="own" if section == "internship" else "na",
        text=text,
        has_number=False,
        source="resume",
        source_doc_id=doc_id,
        source_quote=text,
        span_start=start,
        span_end=start + len(text),
        quote_verified=True,
    )


async def seed(session_factory: Factory) -> tuple[UUID, UUID, UUID]:
    """返回 (user_id, run_id, 被纠正的事实 ID)。"""
    intern_start, school_start = RAW.index(INTERN_HEADING), RAW.index(SCHOOL_HEADING)
    async with session_factory.begin() as session:
        user = User(email="accept@tailor.invalid", hashed_password="test")
        session.add(user)
        await session.flush()
        doc = ResumeDocument(
            user_id=user.user_id,
            raw_text=RAW,
            sections_json=[
                {
                    "name": INTERN,
                    "section": "internship",
                    "start": intern_start,
                    "end": school_start,
                },
                {
                    "name": SCHOOL,
                    "section": "education",
                    "start": school_start,
                    "end": len(RAW),
                },
            ],  # fmt: skip
        )
        fact_set = FactSet(user_id=user.user_id, version=1)
        posting = JobPosting(
            owner_id=user.user_id,
            raw_text="岗位原文",
            content_hash="c" * 64,
            parsed_json=SNAPSHOT,
        )
        session.add_all([doc, fact_set, posting])
        await session.flush()
        corrected = Fact(
            user_id=user.user_id,
            fact_set_id=fact_set.fact_set_id,
            lineage_id=uuid4(),
            experience=INTERN,
            section="internship",
            kind="result",
            responsibility="own",
            text=CORRECTED,
            has_number=True,
            source="user",  # 用户把 30% 纠正为 20%：改了文字就转为用户补充
        )
        session.add_all(
            [
                _resume_fact(
                    user.user_id,
                    fact_set.fact_set_id,
                    doc.doc_id,
                    INTERN,
                    "internship",
                    "负责运单查询接口",
                ),
                corrected,
                _resume_fact(
                    user.user_id,
                    fact_set.fact_set_id,
                    doc.doc_id,
                    SCHOOL,
                    "education",
                    "主修操作系统、编译原理",
                ),
            ]  # fmt: skip
        )
        user_job = UserJob(user_id=user.user_id, posting_id=posting.posting_id)
        session.add(user_job)
        await session.flush()
        run = AiRun(
            user_id=user.user_id,
            kind="tailor",
            user_job_id=user_job.user_job_id,
            fact_set_id=fact_set.fact_set_id,
            base_doc_id=doc.doc_id,
            job_snapshot_json=SNAPSHOT,
        )
        session.add(run)
        await session.flush()
        return user.user_id, run.run_id, corrected.fact_id


class StubbornModel:
    """改写和两次修订都坚持写底稿里的 30%。"""

    def __init__(self, corrected_id: UUID) -> None:
        self.corrected_id = corrected_id
        self.calls: dict[str, int] = {}
        self._kinds = {
            load_prompt(name).text: name
            for name in (
                "match_job",
                "rewrite_bullets",
                "revise_bullet",
                "ground_check",
            )
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        kind = self._kinds[messages[0]["content"]]
        self.calls[kind] = self.calls.get(kind, 0) + 1
        stubborn = {
            "text": f"负责运单查询接口，{OLD_SENTENCE}",
            "fact_ids": ["f1", "f2"],
        }
        reply = {
            "match_job": {
                "items": [
                    {
                        "requirement_id": REQUIREMENT,
                        "verdict": "covered",
                        "fact_ids": [str(self.corrected_id)],
                        "reason": "做过耗时优化",
                        "suggestion": "保留",
                    }
                ],
                "main_gaps": [],
            },
            "rewrite_bullets": {"bullets": [stubborn]},
            "revise_bullet": stubborn,
            "ground_check": {"verdict": "supported", "unsupported_spans": []},
        }[kind]
        return completion_response(json.dumps(reply, ensure_ascii=False))


@pytest.fixture
async def tailored(session_factory: Factory):
    user_id, run_id, corrected_id = await seed(session_factory)
    model = StubbornModel(corrected_id)
    async with fake_upstream(model) as client:
        deps = TailorDeps(
            session_factory=session_factory,
            client=client,
            budget=ModelBudget(remaining_calls=30, remaining_tokens=300_000),
            limiter=asyncio.Semaphore(4),
        )
        await run_tailoring(deps, user_id, run_id)
    return user_id, run_id, model


async def exports(session_factory: Factory, user_id: UUID, run_id: UUID):
    async with session_factory() as session:
        loaded = await load_resume(session, user_id, run_id)
    content = loaded.content
    html = render_html(content, mode=RenderMode.EXPORT, version=1)
    return content, html, to_markdown(content)


def visible(html: str) -> str:
    """去掉样式，只看页面上的文字。"""
    return re.sub(r"<[^>]+>", " ", html.split("</style>", 1)[1])


async def test_only_the_corrected_number_reaches_the_resume(
    session_factory: Factory, tailored
) -> None:
    user_id, run_id, model = tailored
    assert model.calls["revise_bullet"] == 2  # 两次修订都没改掉 30%

    content, html, markdown = await exports(session_factory, user_id, run_id)
    [bullet] = content.sections[0].entries[0].bullets
    assert bullet.origin.value == "fallback"
    assert bullet.text == f"负责运单查询接口；{CORRECTED}"
    for text in (visible(html), markdown):
        assert "20%" in text
        assert "30%" not in text  # 底稿原句和模型的句子都没有进成品


async def test_unrewritten_parts_match_the_base_document(
    session_factory: Factory, tailored
) -> None:
    user_id, run_id, _ = tailored
    content, _, _ = await exports(session_factory, user_id, run_id)
    assert content.header == ["李明", "上海 | liming@example.invalid"]
    [intern_section, school_section] = content.sections
    assert intern_section.title == "实习经历"
    assert school_section.title == "教育经历"
    [school] = school_section.entries
    assert school.heading == SCHOOL_HEADING
    assert (school.title, school.time) == (
        "澄湖大学 | 计算机科学与技术 | 本科",
        "2022.09–2026.06",
    )
    assert school.lines == ["主修操作系统、编译原理"] and school.bullets == []


async def test_uploading_another_resume_does_not_change_the_old_export(
    session_factory: Factory, tailored
) -> None:
    user_id, run_id, _ = tailored
    _, html_before, md_before = await exports(session_factory, user_id, run_id)
    async with session_factory.begin() as session:
        session.add(ResumeDocument(user_id=user_id, raw_text="王五\n全新的简历"))
    _, html_after, md_after = await exports(session_factory, user_id, run_id)
    assert (html_after, md_after) == (html_before, md_before)


async def test_finalize_again_keeps_one_resume_and_one_version(
    session_factory: Factory, tailored
) -> None:
    """模拟 finalize 已提交、检查点没写上就重启后再跑一次 finalize。"""
    user_id, run_id, _ = tailored
    inputs = await load_inputs(session_factory, user_id, run_id)
    with pytest.raises(RunAlreadyFinishedError):
        await save_outcome(
            session_factory,
            user_id,
            run_id,
            inputs=inputs,
            plan=[],
            outcomes=[],
            stop_reason=None,
        )
    async with session_factory() as session:
        resumes = await session.scalar(
            select(func.count()).where(TailoredResume.run_id == run_id)
        )
        versions = await session.scalar(
            select(func.count())
            .select_from(ResumeVersion)
            .join(TailoredResume, TailoredResume.resume_id == ResumeVersion.resume_id)
            .where(TailoredResume.run_id == run_id)
        )
    assert (resumes, versions) == (1, 1)
