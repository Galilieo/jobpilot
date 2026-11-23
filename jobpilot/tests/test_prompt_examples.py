"""提示词里的示例输出要过程序的检查：示例和规则对不上，模型会学到错的做法。"""

import json
import re
from typing import Any

import pytest

from jobpilot.db.models import FactKind, Responsibility
from jobpilot.services.facts import (
    FactExtraction,
    locate_quote,
    numbers_missing_from_quote,
    responsibility_supported,
    role_context,
)
from jobpilot.services.jobs import JobExtraction, build_parsed_job
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
