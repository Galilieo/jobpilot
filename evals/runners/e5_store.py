"""为产品改写建一次性合成账号与输入，采集 ai_run 的实际模型记账。"""

import re
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.runners.e3_dataset import load_resume
from evals.runners.e5_dataset import Pair
from jobpilot.db.models import (
    AiRun,
    Fact,
    FactSet,
    FactSetCriterion,
    FactSetEducation,
    JobPosting,
    ModelCall,
    ResumeDocument,
    User,
    UserJob,
)
from jobpilot.services.facts import locate_quote
from jobpilot.services.resume_content import BaseSection

Factory = async_sessionmaker[AsyncSession]


async def _new_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(
        User(
            user_id=user_id,
            email=f"e5-{user_id.hex}@jobpilot.invalid",
            hashed_password="e5-disabled",
        )
    )
    await session.flush()
    return user_id


def _provenance(doc: ResumeDocument, row: dict[str, Any]) -> dict[str, Any]:
    quote = row["source_quote"]
    spans = locate_quote(doc.raw_text, quote)
    if not spans:
        raise ValueError("合成标注的原文片段无法在底稿定位")
    start, end = spans[0]
    return dict(
        user_id=doc.user_id,
        source="resume",
        source_doc_id=doc.doc_id,
        source_quote=doc.raw_text[start:end],
        span_start=start,
        span_end=end,
        quote_verified=True,
    )


def _sections(pair: Pair, text: str) -> list[dict]:
    """在底稿找经历标题，允许标题中分隔组织和角色的空白不同。"""
    starts = {}
    for fact in pair.facts.values():
        if fact.experience in starts:
            continue
        heading = r"[\s|｜/·]+".join(map(re.escape, fact.experience.split()))
        match = re.search(
            rf"(?m)^[^\S\n]*(?:项目[|｜]|\d{{4}}[^\n]*?\s)?"
            rf"{heading}(?=生?[\s|｜/·，,（(]|$)",
            text,
        )
        if match:
            starts[fact.experience] = (match.start(), fact.section)
        elif fact.section == "skill":
            # 混合栏目没有独立技能标题，从首条技能原文所在行开始。
            row = next(
                row
                for row in pair.annotation["facts"]
                if row["experience"] == fact.experience
            )
            [(offset, _), *_] = locate_quote(text, row["source_quote"])
            starts[fact.experience] = (text.rfind("\n", 0, offset) + 1, fact.section)
    ordered = sorted(
        (start, name, section) for name, (start, section) in starts.items()
    )
    ends = [item[0] for item in ordered[1:]] + [len(text)] if ordered else []
    return [
        BaseSection(name=name, section=section, start=start, end=end).model_dump(
            mode="json"
        )
        for (start, name, section), end in zip(ordered, ends, strict=True)
    ]


def _add_facts(
    session: AsyncSession, pair: Pair, doc: ResumeDocument, fact_set: FactSet
) -> None:
    for ground, row in zip(pair.facts.values(), pair.annotation["facts"], strict=True):
        session.add(
            Fact(
                fact_id=ground.fact_id,
                fact_set_id=fact_set.fact_set_id,
                lineage_id=ground.fact_id,
                experience=ground.experience,
                section=ground.section,
                kind=row["kind"],
                responsibility=ground.responsibility,
                text=ground.text,
                has_number=row["has_number"],
                **_provenance(doc, row),
            )
        )


def _add_criteria(
    session: AsyncSession, pair: Pair, doc: ResumeDocument, fact_set: FactSet
) -> None:
    sample = load_resume(pair.resume_id, doc.user_id)
    for criterion in sample.inputs.criteria:
        row = pair.annotation["criteria"][criterion.field]
        value = row["value"]
        session.add(
            FactSetCriterion(
                fact_set_id=fact_set.fact_set_id,
                field=criterion.field,
                int_value=value if isinstance(value, int) else None,
                text_value=value if isinstance(value, str) else None,
                **_provenance(doc, row),
            )
        )
    for education, row in zip(
        sample.inputs.education, pair.annotation["education"], strict=True
    ):
        fields = {key: value for key, value in row.items() if key != "source_quote"}
        session.add(
            FactSetEducation(
                fact_set_id=fact_set.fact_set_id,
                education_id=UUID(education.ref_id.removeprefix("education.")),
                **fields,
                **_provenance(doc, row),
            )
        )


async def _tailor_run(session: AsyncSession, user_id: UUID, pair: Pair) -> AiRun:
    raw = pair.resume_text
    doc = ResumeDocument(
        user_id=user_id, raw_text=raw, sections_json=_sections(pair, raw)
    )
    fact_set = FactSet(user_id=user_id, version=1)
    snapshot = pair.job.snapshot.model_dump(mode="json")
    jd = pair.jd_text
    posting = JobPosting(
        owner_id=user_id,
        raw_text=jd,
        content_hash=sha256(jd.encode()).hexdigest(),
        parsed_json=snapshot,
    )
    session.add_all([doc, fact_set, posting])
    await session.flush()
    _add_facts(session, pair, doc, fact_set)
    _add_criteria(session, pair, doc, fact_set)
    user_job = UserJob(user_id=user_id, posting_id=posting.posting_id)
    session.add(user_job)
    await session.flush()
    return AiRun(
        user_id=user_id,
        kind="tailor",
        user_job_id=user_job.user_job_id,
        fact_set_id=fact_set.fact_set_id,
        base_doc_id=doc.doc_id,
        job_snapshot_json=snapshot,
    )


async def seed_run(factory: Factory, pair: Pair, *, product: bool) -> tuple[UUID, UUID]:
    async with factory.begin() as session:
        user_id = await _new_user(session)
        run = (
            await _tailor_run(session, user_id, pair)
            if product
            else AiRun(user_id=user_id, kind="tailor")
        )
        session.add(run)
        await session.flush()
        return user_id, run.run_id


async def calls_of(factory: Factory, user_id: UUID) -> list[dict[str, Any]]:
    """每组使用单独账号，按账号取调用能同时计入改写内部的匹配运行。"""
    async with factory() as session:
        calls = await session.scalars(
            select(ModelCall)
            .where(ModelCall.user_id == user_id)
            .order_by(ModelCall.created_at, ModelCall.call_id)
        )
        return [
            dict(
                purpose=call.purpose,
                model=call.model,
                prompt_version=call.prompt_version,
                outcome=call.outcome,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                latency_ms=call.latency_ms,
            )
            for call in calls
        ]


async def remove_run_account(factory: Factory, user_id: UUID) -> None:
    """清理合成账号和简历；model_call 由外键匿名保留。"""
    async with factory.begin() as session:
        await session.delete(await session.get_one(User, user_id))
