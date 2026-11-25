"""提示词里的示例输出要过程序的检查：示例和规则对不上，模型会学到错的做法。"""

import json
import re
from typing import Any
from uuid import UUID, uuid4

import pytest

from jobpilot.db.models import FactKind, FactSource, Responsibility
from jobpilot.services.facts import (
    FactExtraction,
    locate_quote,
    numbers_missing_from_quote,
    responsibility_supported,
    role_context,
)
from jobpilot.services.jobs import JobExtraction, Requirement, build_parsed_job
from jobpilot.services.matching import (
    FactCandidate,
    RawMatchOutput,
    build_match_report,
)
from jobpilot.services.prompts import load_prompt

_RESUME = re.compile(r'<untrusted_data label="resume">\n(.*?)\n</untrusted_data>', re.S)
_OUTPUT = re.compile(r"^输出：\n(\{.*\})$", re.M)


def examples() -> list[tuple[str, dict[str, Any]]]:
    text = load_prompt("extract_facts").text.split("## 示例", 1)[1]
    resumes = _RESUME.findall(text)
    outputs = [json.loads(output) for output in _OUTPUT.findall(text)]
    assert resumes and len(resumes) == len(outputs)
    return list(zip(resumes, outputs, strict=True))


@pytest.mark.parametrize(("raw_text", "output"), examples())
def test_example_output_follows_program_rules(
    raw_text: str, output: dict[str, Any]
) -> None:
    FactExtraction.model_validate(output)  # 字段齐全、取值合法
    for experience in output["experiences"]:
        assert locate_quote(raw_text, experience["heading_quote"])
        for fact in experience["facts"]:
            quote = fact["source_quote"]
            spans = locate_quote(raw_text, quote)
            assert len(spans) == 1, quote
            [(start, end)] = spans
            context = role_context(raw_text, start, end, FactKind(fact["kind"]))
            responsibility = Responsibility(fact["responsibility"])
            assert responsibility_supported(responsibility, context), quote
            assert not numbers_missing_from_quote(fact["text"], quote), quote
            if not fact["responsibility_stated"]:
                assert responsibility is Responsibility.PARTICIPATE, quote
    for item in output["criteria"].values():
        if item is not None:
            assert len(locate_quote(raw_text, item["source_quote"])) == 1
    for edu in output["education"]:
        assert len(locate_quote(raw_text, edu["source_quote"])) == 1


def test_parse_jd_example_follows_program_rules() -> None:
    """JD 解析示例：输出能通过模型校验，每条要求和职责的片段都在示例 JD 里。"""
    text = load_prompt("parse_jd").text.split("## 示例", 1)[1]
    [raw_text] = re.findall(
        r'<untrusted_data label="jd">\n(.*?)\n</untrusted_data>', text, re.S
    )
    [output] = _OUTPUT.findall(text)

    parsed = build_parsed_job(raw_text, JobExtraction.model_validate_json(output))

    assert parsed.requirements and parsed.duties
    assert all(item.quote_verified for item in [*parsed.requirements, *parsed.duties])


def test_match_job_example_follows_program_rules() -> None:
    """匹配分析示例：输出通过模型与业务规则校验，且覆盖硬性与加分项。"""
    text = load_prompt("match_job").text.split("## 示例", 1)[1]
    [job_raw] = re.findall(
        r'<untrusted_data label="job_requirements">\n(.*?)\n</untrusted_data>',
        text,
        re.S,
    )
    [facts_raw] = re.findall(
        r'<untrusted_data label="facts">\n(.*?)\n</untrusted_data>', text, re.S
    )
    [output] = _OUTPUT.findall(text)

    user_id, fact_set_id = uuid4(), uuid4()
    requirements = [
        Requirement.model_validate(item) for item in json.loads(job_raw)["requirements"]
    ]
    facts = [
        FactCandidate(
            fact_id=UUID(item["fact_id"]),
            user_id=user_id,
            fact_set_id=fact_set_id,
            lineage_id=uuid4(),
            experience=item["experience"],
            kind=FactKind(item["kind"]),
            responsibility=Responsibility(item["responsibility"]),
            text=item["text"],
            source=FactSource.RESUME,
            confirmed=True,
        )
        for item in json.loads(facts_raw)
    ]

    built = build_match_report(
        user_id=user_id,
        fact_set_id=fact_set_id,
        requirements=requirements,
        facts=facts,
        raw=RawMatchOutput.model_validate_json(output),
    )

    assert len(built.items) == len(requirements)
    assert built.hard_summary.total == 2
    assert built.hard_summary.covered == 1
    assert built.hard_summary.partial == 1
    assert built.preferred_summary.total == 1
    assert built.preferred_summary.missing == 1
