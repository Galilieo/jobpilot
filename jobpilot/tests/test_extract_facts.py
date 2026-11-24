"""事实拆分全流程：假上游返回预设 JSON，网关、分段、原文定位和程序规则都真实执行。

调用记录写入真实测试库。简历是合成的：两段实习写了同一句话，用来验证段内定位。
"""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import AiRun, CallOutcome, Responsibility, User
from jobpilot.services.facts import (
    ExtractionResult,
    ReviewReason,
    TruncatedOutputError,
    extract_facts,
)
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.tests.test_call_accounting import (
    calls_of,
    completion_response,
    fake_upstream,
)

Factory = async_sessionmaker[AsyncSession]

RESUME = (
    "张三  13800000000\n"
    "【实习经历】\n"
    "甲公司 后端开发实习生 2024.07-2024.09\n"
    "负责后端接口开发\n"
    "使用 FastAPI 编写 12 个接口\n"
    "乙公司 后端开发实习生 2025.01-2025.03\n"
    "负责后端接口开发\n"
    "优化慢查询，响应时间\n"
    "降低 30%\n"
    "【技能】\n"
    "技能：Python、FastAPI\n"
)
JIA = "甲公司 后端开发实习生 2024.07-2024.09"
YI = "乙公司 后端开发实习生 2025.01-2025.03"
SKILLS = "技能：Python、FastAPI"
DOC_ID = uuid4()


def fact(text: str, quote: str, **overrides: Any) -> dict[str, Any]:
    return {
        "kind": "action",
        "responsibility": "own",
        "responsibility_stated": True,
        "text": text,
        "has_number": False,
        "source_quote": quote,
    } | overrides


def experience(
    name: str, section: str, heading: str, *facts: dict[str, Any]
) -> dict[str, Any]:
    return {
        "name": name,
        "section": section,
        "heading_quote": heading,
        "facts": list(facts),
    }


@pytest.fixture
async def run(session_factory: Factory) -> AiRun:
    async with session_factory.begin() as session:
        owner = User(email="facts@example.invalid", hashed_password="test")
        session.add(owner)
        await session.flush()
        run = AiRun(user_id=owner.user_id, kind="parse_resume")
        session.add(run)
    return run


async def extract(
    session_factory: Factory,
    run: AiRun,
    *experiences: dict[str, Any],
    finish_reason: str = "stop",
    sent: list[dict[str, Any]] | None = None,
) -> ExtractionResult:
    """让假上游返回给定的经历列表，sent 收集网关实际发出的请求体。"""
    output = json.dumps({"experiences": list(experiences)}, ensure_ascii=False)

    def respond(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return completion_response(output, finish_reason=finish_reason)

    async with fake_upstream(respond) as client:
        return await extract_facts(
            session_factory,
            run.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=10, remaining_tokens=100_000),
            doc_id=DOC_ID,
            raw_text=RESUME,
        )


# ---------- 分段 ----------


async def test_sections_are_cut_at_located_headings(
    session_factory: Factory, run: AiRun
) -> None:
    result = await extract(
        session_factory,
        run,
        experience("甲公司实习", "internship", JIA),
        experience("乙公司实习", "internship", YI),
        experience("技能", "skill", SKILLS),
    )

    assert [(s.name, s.start, s.end) for s in result.sections] == [
        ("甲公司实习", RESUME.index(JIA), RESUME.index(YI)),
        ("乙公司实习", RESUME.index(YI), RESUME.index(SKILLS)),
        ("技能", RESUME.index(SKILLS), len(RESUME)),
    ]


async def test_heading_not_found_falls_back_to_whole_text(
    session_factory: Factory, run: AiRun
) -> None:
    """标题对不上时这段经历没有范围，事实在全文中找；出现多次取第一处并提醒。"""
    result = await extract(
        session_factory,
        run,
        experience(
            "乙公司实习",
            "internship",
            "乙公司 实习",  # 原文里没有这个写法
            fact("负责后端接口开发", "负责后端接口开发"),
        ),
    )

    assert result.sections == []
    [draft] = result.facts
    assert draft.quote_verified
    assert draft.span_start == RESUME.index("负责后端接口开发")  # 第一处，在甲公司
    assert set(draft.review_reasons) == {
        ReviewReason.QUOTE_AMBIGUOUS,
        ReviewReason.EXPERIENCE_UNCERTAIN,
    }


# ---------- 原文定位 ----------


async def test_same_sentence_is_located_in_its_own_section(
    session_factory: Factory, run: AiRun
) -> None:
    result = await extract(
        session_factory,
        run,
        experience(
            "甲公司实习",
            "internship",
            JIA,
            fact("负责后端接口开发", "负责后端接口开发"),
        ),
        experience(
            "乙公司实习", "internship", YI, fact("负责后端接口开发", "负责后端接口开发")
        ),
    )

    jia, yi = result.facts
    assert jia.span_start == RESUME.index("负责后端接口开发")
    assert yi.span_start == RESUME.index("负责后端接口开发", RESUME.index(YI))
    for draft in (jia, yi):
        assert draft.quote_verified
        assert draft.review_reasons == []
        assert draft.source_doc_id == DOC_ID
        assert RESUME[draft.span_start : draft.span_end] == draft.source_quote


async def test_quote_from_another_section_is_not_verified(
    session_factory: Factory, run: AiRun
) -> None:
    """片段其实在甲公司那一段：不退回全文找，判为未通过，交给用户核对。"""
    quote = "使用 FastAPI 编写 12 个接口"
    result = await extract(
        session_factory,
        run,
        experience("甲公司实习", "internship", JIA),
        experience(
            "乙公司实习",
            "internship",
            YI,
            fact("负责编写接口", quote, kind="technology"),
        ),
    )

    [draft] = result.facts
    assert not draft.quote_verified
    assert (draft.span_start, draft.span_end) == (None, None)
    assert draft.source_quote == quote  # 保留模型给的文字供用户对照
    assert set(draft.review_reasons) == {ReviewReason.QUOTE_NOT_FOUND}


async def test_verified_quote_is_taken_from_the_original_text(
    session_factory: Factory, run: AiRun
) -> None:
    """模型抄写时漏了换行：仍然通过，保存的片段是原文里带换行的那一段。"""
    result = await extract(
        session_factory,
        run,
        experience(
            "乙公司实习",
            "internship",
            YI,
            # 这里只测片段按原文保存；职责标为参与，不让职责检查掺进来。
            # 原文在「响应时间」后硬换行，结果只认所在的句子，按 own 会被标出。
            fact(
                "优化慢查询，响应时间降低 30%",
                "负责后端接口开发\n优化慢查询，响应时间降低 30%",
                kind="result",
                responsibility="participate",
            ),
        ),
    )

    [draft] = result.facts
    assert draft.quote_verified
    assert draft.source_quote == "负责后端接口开发\n优化慢查询，响应时间\n降低 30%"
    assert draft.review_reasons == []


# ---------- 程序强制的规则 ----------


async def test_program_rules_override_model_labels(
    session_factory: Factory, run: AiRun
) -> None:
    result = await extract(
        session_factory,
        run,
        experience(
            "乙公司实习",
            "internship",
            YI,
            # 原文没说明职责：改为 participate 并提醒
            fact("负责后端接口开发", "负责后端接口开发", responsibility_stated=False),
            # 职责和数字都比片段夸大
            fact(
                "主导优化慢查询，响应时间降低 50%",
                "优化慢查询，响应时间降低 30%",
                kind="result",
                responsibility="lead",
            ),
        ),
        experience(
            "技能",
            "skill",
            SKILLS,
            # 技能段落不区分职责，模型给的 lead 一律改为 na
            fact(
                "熟悉 Python", "技能：Python", kind="technology", responsibility="lead"
            ),
        ),
    )

    unstated, exaggerated, skill = result.facts
    assert unstated.responsibility is Responsibility.PARTICIPATE
    assert set(unstated.review_reasons) == {ReviewReason.RESPONSIBILITY_UNSTATED}

    assert exaggerated.quote_verified  # 片段是真的，问题出在转述
    assert exaggerated.has_number  # 模型说没有数字，程序发现了 50
    assert set(exaggerated.review_reasons) == {
        ReviewReason.RESPONSIBILITY_UNSUPPORTED,
        ReviewReason.NUMBER_NOT_IN_QUOTE,
    }

    assert skill.responsibility is Responsibility.NA
    assert skill.review_reasons == []


# ---------- 与网关的配合 ----------


async def test_request_wraps_and_redacts_resume_and_is_recorded(
    session_factory: Factory, run: AiRun
) -> None:
    sent: list[dict[str, Any]] = []

    await extract(session_factory, run, sent=sent)

    [body] = sent
    system, user = body["messages"]
    assert system == {"role": "system", "content": load_prompt("extract_facts").text}
    assert user["content"].startswith('<untrusted_data label="resume">')
    assert "13800000000" not in user["content"]  # 网关外发前遮蔽了手机号
    assert "[PHONE]" in user["content"]
    assert body["response_format"] == {"type": "json_object"}

    [call] = await calls_of(session_factory, run)
    assert (call.purpose, call.prompt_version, call.outcome) == (
        "extract_facts",
        load_prompt("extract_facts").label,
        CallOutcome.SUCCESS,
    )


async def test_truncated_output_fails_without_repair(
    session_factory: Factory, run: AiRun
) -> None:
    sent: list[dict[str, Any]] = []

    with pytest.raises(TruncatedOutputError):
        await extract(session_factory, run, finish_reason="length", sent=sent)

    assert len(sent) == 1  # 没有触发 JSON 修复重发
    [call] = await calls_of(session_factory, run)
    assert call.outcome == CallOutcome.INVALID_OUTPUT
