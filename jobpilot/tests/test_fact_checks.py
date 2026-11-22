"""转述检查与模型输出解析：纯函数，不连数据库、不调用模型。"""

import json

import pytest
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from jobpilot.db.models import Responsibility
from jobpilot.services.facts import (
    FactExtraction,
    TruncatedOutputError,
    numbers_missing_from_quote,
    parse_extraction,
    responsibility_supported,
)

VALID_OUTPUT = {
    "experiences": [
        {
            "name": "课程排班面板项目",
            "section": "project",
            "heading_quote": "课程排班面板项目 2025.03-2025.05",
            "facts": [
                {
                    "kind": "action",
                    "responsibility": "own",
                    "responsibility_stated": True,
                    "text": "实现按周次筛选课程的功能",
                    "has_number": False,
                    "source_quote": "负责实现按周次筛选课程的功能",
                }
            ],
        }
    ]
}


def completion(content: str, finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ],
        }
    )


# ---------- 解析模型输出 ----------


def test_valid_output_is_parsed() -> None:
    result = parse_extraction(completion(json.dumps(VALID_OUTPUT, ensure_ascii=False)))

    assert isinstance(result, FactExtraction)
    assert result.experiences[0].facts[0].responsibility is Responsibility.OWN


def test_truncated_output_is_not_a_validation_error() -> None:
    """截断不交给网关的 JSON 修复：抛出的不能是 ValidationError。"""
    cut = json.dumps(VALID_OUTPUT, ensure_ascii=False)[:60]

    with pytest.raises(TruncatedOutputError):
        parse_extraction(completion(cut, finish_reason="length"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o["experiences"][0]["facts"][0].update(kind="skill"),
        lambda o: o["experiences"][0]["facts"][0].update(confidence=0.9),
        lambda o: o["experiences"][0].update(section="work"),
        lambda o: o["experiences"][0]["facts"][0].update(source_quote=""),
    ],
    ids=["unknown-kind", "extra-field", "unknown-section", "empty-quote"],
)
def test_malformed_output_raises_validation_error(mutate) -> None:
    """这些错误交给网关修复重发一次。"""
    output = json.loads(json.dumps(VALID_OUTPUT))
    mutate(output)

    with pytest.raises(ValidationError):
        parse_extraction(completion(json.dumps(output, ensure_ascii=False)))


# ---------- 角色词 ----------


@pytest.mark.parametrize(
    ("responsibility", "quote", "supported"),
    [
        (Responsibility.LEAD, "主导订单系统重构", True),
        (Responsibility.LEAD, "担任项目负责人", True),
        (Responsibility.LEAD, "负责订单模块开发", False),  # 「负责」只够 own
        (Responsibility.LEAD, "参与数据清洗", False),
        (Responsibility.OWN, "负责订单模块开发", True),
        (Responsibility.OWN, "牵头完成迁移", True),  # lead 级的词也支持 own
        (Responsibility.OWN, "使用 FastAPI 编写接口", False),
        (Responsibility.PARTICIPATE, "使用 FastAPI 编写接口", True),
        (Responsibility.NA, "本科 计算机科学与技术", True),
    ],
)
def test_responsibility_needs_role_word_of_its_level(
    responsibility: Responsibility, quote: str, supported: bool
) -> None:
    assert responsibility_supported(responsibility, quote) is supported


# ---------- 数字 ----------


@pytest.mark.parametrize(
    ("text", "quote", "missing"),
    [
        ("响应时间降低 30%", "优化慢查询，响应时间降低 30%", []),
        ("响应时间降低 50%", "优化慢查询，响应时间降低 30%", ["50"]),
        ("编写 5 个接口", "编写 50 个接口", ["5"]),  # 按整个数字比较，不按子串
        ("提升 1.5 倍", "提升 1.5 倍", []),
        ("提升 1.5 倍", "提升 15 倍", ["1.5"]),
        ("降低３０%", "降低 30%", []),  # 全角数字先转半角
        ("提升三成", "提升三成", []),  # 中文数字不在检查范围
    ],
)
def test_numbers_in_text_must_appear_in_quote(
    text: str, quote: str, missing: list[str]
) -> None:
    assert numbers_missing_from_quote(text, quote) == missing
