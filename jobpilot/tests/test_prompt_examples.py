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
