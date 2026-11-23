"""JD 里的提示注入检测：按规则找出常见的注入句式，只标记、不阻断。

标记的作用是提醒用户和留档；真正的防线在别处：JD 包在不可信数据区块里，
解析结果的每条要求都要在原文中找到片段，改写只能引用用户确认过的事实。
规则宁缺毋滥：每条规则都在真实 JD 上检查过误报（见 tests/test_injection.py）。
"""

import re
from dataclasses import asdict, dataclass
from typing import Any

# 片段前后各取这么多字符，方便用户看到上下文。
_CONTEXT = 20

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"(忽略|无视|忽视|不要理会|别管)(掉)?(以上|上面|前面|之前|此前|先前|"
            r"所有|全部|一切)?的?(所有|全部)?(指令|指示|规则|提示词?|设定)"
        ),
    ),
    (
        "ignore_instructions",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,30}\b(previous|above|prior|"
            r"earlier|all)\b[^.\n]{0,30}\b(instructions?|prompts?|rules?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_play",
        re.compile(r"你现在是|你现在扮演|请你扮演|从现在开始你是|你的新(身份|任务)是"),
    ),
    (
        "role_play",
        re.compile(r"\b(you are now|pretend to be|from now on,? you)\b", re.IGNORECASE),
    ),
    (
        "addressing_ai",
        re.compile(
            r"(如果你是|假如你是|if you are an?)\s*(AI|人工智能|大模型|语言模型|LLM|"
            r"language model|chatbot|自动化工具)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt",
        re.compile(
            r"system\s*prompt|系统提示词|开发者模式|developer mode", re.IGNORECASE
        ),
    ),
    (
        "write_into_output",
        re.compile(
            r"(在|往)(你的|候选人的?|生成的|输出的)?(简历|回答|输出|回复)(中|里)"
            r"(加入|写上|添加|注明|包含|插入)"
        ),
    ),
    ("data_tag", re.compile(r"<\s*/?\s*untrusted_data", re.IGNORECASE)),
    (
        "chat_role_marker",
        re.compile(r"^\s*(system|assistant)\s*[:：]", re.IGNORECASE | re.MULTILINE),
    ),
)


@dataclass(frozen=True)
class InjectionFlag:
    """命中的一条规则：规则名、命中的文字、带上下文的片段和位置。"""

    rule: str
    match: str
    context: str
    start: int
    end: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def detect_injection(text: str) -> list[InjectionFlag]:
    """返回全部命中，按位置排序；同一位置被多条规则命中只保留一条。"""
    flags: dict[int, InjectionFlag] = {}
    for rule, pattern in _RULES:
        for found in pattern.finditer(text):
            start, end = found.span()
            if start in flags:
                continue
            context = text[max(0, start - _CONTEXT) : end + _CONTEXT]
            flags[start] = InjectionFlag(rule, found.group(), context, start, end)
    return [flags[start] for start in sorted(flags)]
