"""依据检查第一、二层：纯函数，不访问数据库、不调用模型，同样的输入总得到同样的结果。

第一层（结构）：至少引用一条事实；引用的事实在本次可用范围内；来自简历的事实通过了原文校验；
经历条目引用的事实属于同一段经历。
第二层（数字与实体）：改写里的数字、技术名、组织名、角色词、程度词，都不能超出引用的事实。

「本次可用范围」由调用方决定：只放入当前用户、本次运行固定的 fact_set_id 下的事实。
不在范围内的 ID 一律当作不可用，不区分是不存在、属于别人还是属于别的版本，
避免泄露其他用户的数据。

设计见 notes/求职助手Agent/03-可信安全与隐私.md 第 1 节。已知局限：
中文数字（「三成」）不检查；同义词表里没有的技术名识别不出；只认得事实库里出现过的组织名；
角色词和程度词只看是否出现，「不负责」也算出现了「负责」，
照抄事实里提到别人的角色词（「负责人」）也会放过；隐含的夸大由第三层（模型）检查。
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import cache
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from jobpilot.db.models import (
    BulletSection,
    Fact,
    FactSource,
    Responsibility,
    SectionKind,
)
from jobpilot.services.grounding_rules import (
    RESPONSIBILITY_RANK,
    GroundingRules,
    default_rules,
)
from jobpilot.services.keywords import default_table


class IssueKind(StrEnum):
    # 第一层：结构
    MISSING_CITATION = "missing_citation"
    UNAVAILABLE_FACT = "unavailable_fact"
    UNVERIFIED_QUOTE = "unverified_quote"
    CROSS_EXPERIENCE = "cross_experience"  # 张冠李戴
    # 第二层：数字与实体
    NUMBER = "number"  # 数字编造或放大
    TECHNOLOGY = "technology"  # 凭空添加
    ORGANIZATION = "organization"  # 基本信息改动、张冠李戴
    ROLE = "role"  # 角色升级
    DEGREE = "degree"  # 程度升级
    # 第三层：语义（services/semantic_check.py）
    SEMANTIC = "semantic"  # 模型判断为部分支持或不支持
    CHECK_UNAVAILABLE = "check_unavailable"  # 第三层没能完成，按未通过处理


STRUCTURE_KINDS = frozenset(
    {
        IssueKind.MISSING_CITATION,
        IssueKind.UNAVAILABLE_FACT,
        IssueKind.UNVERIFIED_QUOTE,
        IssueKind.CROSS_EXPERIENCE,
    }
)


class GroundingIssue(BaseModel):
    """一处问题。message 给用户看，也作为修订时的问题清单交给模型。"""

    model_config = ConfigDict(frozen=True)

    kind: IssueKind
    message: str
    fragment: str | None = None  # 改写里出问题的文字


@dataclass(frozen=True)
class GroundingFact:
    """依据检查需要的事实字段。"""

    fact_id: UUID
    experience: str
    responsibility: Responsibility
    text: str
    source: FactSource
    quote_verified: bool
    section: SectionKind | None = None  # 用户补充、旧版本的事实可能没有

    @classmethod
    def of(cls, fact: Fact) -> "GroundingFact":
        return cls(
            fact_id=fact.fact_id,
            experience=fact.experience,
            responsibility=Responsibility(fact.responsibility),
            text=fact.text,
            source=FactSource(fact.source),
            quote_verified=fact.quote_verified,
            section=SectionKind(fact.section) if fact.section else None,
        )


@dataclass(frozen=True)
class GroundingResult:
    issues: tuple[GroundingIssue, ...]

    @property
    def passed(self) -> bool:
        return not self.issues


def check_grounding(
    text: str,
    fact_ids: Sequence[UUID],
    available: Mapping[UUID, GroundingFact],
    *,
    section: BulletSection,
    experience: str | None = None,
    known_organizations: Iterable[str] = (),
) -> GroundingResult:
    """对一条改写依次做第一、二层检查，返回全部问题。

    experience：经历条目所属的经历名；给出时，引用的事实都必须来自这段经历。
    known_organizations：用户事实库里出现过的组织名，见 organizations_of。
    """
    issues = check_structure(
        fact_ids, available, section=section, experience=experience
    )
    cited = _cited(fact_ids, available)
    if cited:
        issues += check_content(text, cited, known_organizations=known_organizations)
    return GroundingResult(tuple(issues))


def should_run_semantic(
    issues: Sequence[GroundingIssue], *, revisions_left: int
) -> bool:
    """第一、二层的结果出来后，决定这一轮要不要再调第三层（模型）。

    - 第一层不过：引用本身有问题，判断语义没有意义，不调。
    - 第一、二层都过：调。
    - 只有第二层不过：默认不调，反正要修订，省一次调用；
      但最后一次修订之前照样调，把问题一次给全，
      免得最后一次修订只修好了数字，又被第三层拦下却没有机会再改。
    revisions_left：这次检查之后还允许修订几次。
    """
    if any(issue.kind in STRUCTURE_KINDS for issue in issues):
        return False
    return not issues or revisions_left == 1


# ---------- 第一层：结构 ----------


def _cited(
    fact_ids: Sequence[UUID], available: Mapping[UUID, GroundingFact]
) -> list[GroundingFact]:
    """引用里在可用范围内的事实，去重并保持顺序。"""
    return [available[i] for i in dict.fromkeys(fact_ids) if i in available]


def check_structure(
    fact_ids: Sequence[UUID],
    available: Mapping[UUID, GroundingFact],
    *,
    section: BulletSection,
    experience: str | None = None,
) -> list[GroundingIssue]:
    if not fact_ids:
        return [
            GroundingIssue(kind=IssueKind.MISSING_CITATION, message="没有引用任何事实")
        ]

    issues = []
    cited = []
    for fact_id in dict.fromkeys(fact_ids):
        fact = available.get(fact_id)
        if fact is None:
            issues.append(
                GroundingIssue(
                    kind=IssueKind.UNAVAILABLE_FACT,
                    message=f"引用的事实 {fact_id} 不在本次使用的事实库里",
                )
            )
            continue
        cited.append(fact)
        if fact.source is FactSource.RESUME and not fact.quote_verified:
            issues.append(
                GroundingIssue(
                    kind=IssueKind.UNVERIFIED_QUOTE,
                    message=f"引用的事实「{fact.text}」没有通过原文校验",
                )
            )

    if section is BulletSection.EXPERIENCE and cited:
        experiences = sorted({fact.experience for fact in cited})
        if experience is not None and experiences != [experience]:
            others = [name for name in experiences if name != experience]
            issues.append(
                GroundingIssue(
                    kind=IssueKind.CROSS_EXPERIENCE,
                    message=f"这条写在「{experience}」下，却引用了"
                    + "、".join(f"「{name}」" for name in others)
                    + "的事实",
                )
            )
        elif len(experiences) > 1:
            issues.append(
                GroundingIssue(
                    kind=IssueKind.CROSS_EXPERIENCE,
                    message="一条经历条目引用了多段经历的事实："
                    + "、".join(f"「{name}」" for name in experiences),
                )
            )
    return issues


# ---------- 第二层：数字与实体 ----------


def check_content(
    text: str,
    cited: Sequence[GroundingFact],
    *,
    known_organizations: Iterable[str] = (),
    rules: GroundingRules | None = None,
    keyword_table: Mapping[str, str] | None = None,
) -> list[GroundingIssue]:
    rules = rules or default_rules()
    keyword_table = default_table() if keyword_table is None else keyword_table
    fact_text = "\n".join(fact.text for fact in cited)
    # 经历名本身（如「Redis 缓存优化项目」）也算依据，技术名和组织名可以从这里来。
    context = fact_text + "\n" + "\n".join({fact.experience for fact in cited})

    return [
        *_check_numbers(text, fact_text, rules),
        *_check_technologies(text, context, keyword_table),
        *_check_organizations(text, context, known_organizations),
        *_check_roles(text, cited, rules),
        *_check_degrees(text, fact_text, rules),
    ]


# 这几类经历的名字以组织或项目开头（学校、公司、项目名、社团）；
# 技能、获奖的经历名是栏目名（「技能」「工具与技能」），不是组织。
ORGANIZATION_SECTIONS = frozenset(
    {
        SectionKind.EDUCATION,
        SectionKind.INTERNSHIP,
        SectionKind.PROJECT,
        SectionKind.OTHER,
    }
)


def organizations_of(facts: Iterable[GroundingFact]) -> set[str]:
    """从用户事实库取出组织名：上面几类经历的经历名的第一个词。

    例如「云栈物流科技 后端开发实习」→「云栈物流科技」。
    少于 2 个字的不算，避免把「A 项目」里的「A」当成组织名。
    一段经历只要有一条事实带着这几类段落类型就算；全部没有段落类型的经历不取，
    宁可少认一个组织名（交给第三层），也不把栏目名当成组织去误拦。
    """
    experiences = {
        fact.experience for fact in facts if fact.section in ORGANIZATION_SECTIONS
    }
    first_words = (name.split()[0] for name in experiences if name.strip())
    return {word for word in first_words if len(word) >= 2}


# 数字：前面紧挨着英文字母或数字的不算（P95、Vue3、H5 属于名字，交给技术名检查）。
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d+(?:,\d{3})*(?:\.\d+)?(?![\d])")
_COUNT = "count"
_CALENDAR_MONTH = "calendar_month"  # 「3 月」是三月；时长写「3 个月」，单位是 month


def _normalize(text: str) -> str:
    # 全角数字、全角百分号转成半角。
    return unicodedata.normalize("NFKC", text)


def _unit_after(text: str, end: int, rules: GroundingRules) -> tuple[str, int]:
    """看数字后面跟的单位，返回 (规范单位, 单位在原文中的结束位置)。

    没列出的单位都算计数。
    """
    rest = text[end:]
    stripped = rest.lstrip(" ")
    start = end + len(rest) - len(stripped)
    for candidate in rules.units_longest_first:
        if not stripped.startswith(candidate):
            continue
        after = stripped[len(candidate) : len(candidate) + 1]
        # 英文单位后面不能紧跟字母：「5 sessions」里的 s 不是秒。
        if (
            candidate.isascii()
            and candidate.isalpha()
            and after.isascii()
            and after.isalpha()
        ):
            continue
        return rules.number_units[candidate], start + len(candidate)
    return _COUNT, end


def _canonical(value: Decimal, unit: str) -> tuple[str, str]:
    """数值去掉多余的零；1900～2099 的整数当作年份，「2025」和「2025 年」算同一个。"""
    if (
        unit in (_COUNT, "year")
        and value == value.to_integral()
        and 1900 <= value < 2100
    ):
        unit = "year"
    return format(value.normalize(), "f"), unit


# 年月：2025.03、2025-03、2025/3、2025 年 3 月。「2025 年 3 个月」里的 3 是时长，不算。
_YEAR_MONTH = re.compile(
    r"(?<![\d.])((?:19|20)\d{2})\s*(?:[./-]|年)\s*(\d{1,2})(?!\d)(?!\s*个)(?:\s*月)?"
)


def _year_months(text: str) -> tuple[list[tuple[str, str, str]], str]:
    """先认出年月，拆成年份和月份两个数；返回它们和挖掉年月后的文字。

    拆开比较，事实写「2025.03-2025.06」，改写写「2025 年 3 月至 6 月」也能对上；
    不拆的话「2025.03」会被当成小数 2025.03。
    """
    found = []

    def take(match: re.Match[str]) -> str:
        fragment = match.group()
        found.append((fragment, str(int(match.group(1))), "year"))
        found.append((fragment, str(int(match.group(2))), _CALENDAR_MONTH))
        return " " * len(fragment)  # 保持长度，后面的位置不变

    return found, _YEAR_MONTH.sub(take, text)


def _numbers(text: str, rules: GroundingRules) -> list[tuple[str, str, str]]:
    """返回 (原文片段, 规范数值, 规范单位)。只认阿拉伯数字。"""
    found, text = _year_months(_normalize(text))
    for match in _NUMBER.finditer(text):
        unit, unit_end = _unit_after(text, match.end(), rules)
        value, unit = _canonical(Decimal(match.group().replace(",", "")), unit)
        found.append((text[match.start() : unit_end], value, unit))
    return found


_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                   "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}  # fmt: skip
_CHINESE_MAGNITUDES = {"十": 10, "百": 100, "千": 1000}
_CHINESE_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百千]+")


def _chinese_value(word: str) -> int | None:
    """中文数字转整数，认不出返回 None。

    「两」→2、「十二」→12、「二十」→20、「百」→100、「一百二十」→120。
    """
    total, digit = 0, None
    for char in word:
        if char in "零〇":  # 「一百零五」里的零只是占位
            continue
        if char in _CHINESE_DIGITS:
            if digit is not None:  # 「一二」这种连写不是数字
                return None
            digit = _CHINESE_DIGITS[char]
        else:
            total += (1 if digit is None else digit) * _CHINESE_MAGNITUDES[char]
            digit = None
    return total + (digit or 0)


def _chinese_numbers(text: str, rules: GroundingRules) -> list[tuple[str, str]]:
    """事实里写在量词前的中文数字：「两种」「四人」「前十位」「每百张」。

    只用来扩充「依据里有的数字」，不检查改写里的中文数字：
    改写写「一次性」不该被当成数字 1。
    后面不接量词或单位的（「一起」「一些」）不算。
    """
    text = _normalize(text)
    found = []
    for match in _CHINESE_NUMBER.finditer(text):
        if not text.startswith(rules.chinese_classifiers, match.end()):
            continue
        number = _chinese_value(match.group())
        if number is None:
            continue
        unit, _ = _unit_after(text, match.end(), rules)
        found.append(_canonical(Decimal(number), unit))
    return found


def _check_numbers(
    text: str, fact_text: str, rules: GroundingRules
) -> list[GroundingIssue]:
    supported = {(value, unit) for _, value, unit in _numbers(fact_text, rules)}
    supported |= set(_chinese_numbers(fact_text, rules))
    # 年月拆成两个数，年和月都对不上时同一个片段只报一次。
    unsupported = dict.fromkeys(
        fragment
        for fragment, value, unit in _numbers(text, rules)
        if (value, unit) not in supported
    )
    return [
        GroundingIssue(
            kind=IssueKind.NUMBER,
            message=f"数字「{fragment}」在引用的事实里找不到（数值和单位都要一致）",
            fragment=fragment,
        )
        for fragment in unsupported
    ]


@cache
def _keyword_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    def alternative(word: str) -> str:
        # 英文词两端不能紧挨英文字母或数字：
        # Java 不能命中 JavaScript，Go 不能命中 Google。
        left = r"(?<![a-z0-9])" if word[0].isascii() and word[0].isalnum() else ""
        right = r"(?![a-z0-9])" if word[-1].isascii() and word[-1].isalnum() else ""
        return left + re.escape(word) + right

    # 同一位置取最长的：「多模态大模型」不会被拆成「大模型」。
    ordered = sorted(words, key=len, reverse=True)
    return re.compile("|".join(alternative(word) for word in ordered))


def technologies_in(text: str, keyword_table: Mapping[str, str]) -> dict[str, str]:
    """返回 {标准名: 原文写法}。按同义词表识别，表里没有的技术名识别不出。"""
    if not keyword_table:
        return {}
    original = _normalize(text)
    normalized = original.casefold()
    # 大小写转换一般不改变长度；改变时退回小写写法，只影响报告里显示的片段。
    same_length = len(normalized) == len(original)
    pattern = _keyword_pattern(tuple(keyword_table))
    found: dict[str, str] = {}
    for match in pattern.finditer(normalized):
        written = (
            original[match.start() : match.end()] if same_length else match.group()
        )
        found.setdefault(keyword_table[match.group()], written)
    return found


def _check_technologies(
    text: str, context: str, keyword_table: Mapping[str, str]
) -> list[GroundingIssue]:
    supported = technologies_in(context, keyword_table)
    return [
        GroundingIssue(
            kind=IssueKind.TECHNOLOGY,
            message=f"技术「{name}」在引用的事实里没有出现",
            fragment=written,
        )
        for name, written in technologies_in(text, keyword_table).items()
        if name not in supported
    ]


def _check_organizations(
    text: str, context: str, known_organizations: Iterable[str]
) -> list[GroundingIssue]:
    text, context = _normalize(text), _normalize(context)
    return [
        GroundingIssue(
            kind=IssueKind.ORGANIZATION,
            message=f"「{name}」属于另一段经历，引用的事实里没有它",
            fragment=name,
        )
        for name in sorted(set(known_organizations))
        if name in text and name not in context
    ]


def _fact_role_rank(fact: GroundingFact, rules: GroundingRules) -> int | None:
    """这条事实允许的最高角色等级；没有等级时返回 None。

    有职责程度的看 responsibility。职责不适用（教育、志愿者、技能……）的看原文：
    取原文角色词里最低的等级，「协助负责人整理清单」按「协助」算，
    不能因为提到了「负责人」就允许写「主导」。
    """
    if fact.responsibility in RESPONSIBILITY_RANK:
        return RESPONSIBILITY_RANK[fact.responsibility]
    stated = [rules.role_rank(word) for word in rules.role_pattern.findall(fact.text)]
    return min(stated, default=None)


def _check_roles(
    text: str, cited: Sequence[GroundingFact], rules: GroundingRules
) -> list[GroundingIssue]:
    """改写里的角色词：事实原文写了同一个词就可以用，否则不能高于事实的等级。

    「同一个词」让「和测试负责人商量」这类提到别人的角色词能照抄；
    照抄后意思变没变（「作为负责人」）由第三层判断。
    """
    words = list(dict.fromkeys(rules.role_pattern.findall(text)))
    if not words:
        return []
    stated = {word for fact in cited for word in rules.role_pattern.findall(fact.text)}
    ranks = [
        rank for fact in cited if (rank := _fact_role_rank(fact, rules)) is not None
    ]
    allowed = max(ranks, default=-1)
    if any(fact.responsibility in RESPONSIBILITY_RANK for fact in cited):
        template = "「{}」高于引用事实的职责程度"
    else:
        template = "引用的事实不涉及职责程度，原文也没有这么写，改写里不能出现「{}」"
    return [
        GroundingIssue(
            kind=IssueKind.ROLE, message=template.format(word), fragment=word
        )
        for word in words
        if word not in stated and rules.role_rank(word) > allowed
    ]


def _check_degrees(
    text: str, fact_text: str, rules: GroundingRules
) -> list[GroundingIssue]:
    words = list(dict.fromkeys(rules.degree_pattern.findall(text)))
    if not words:
        return []
    allowed = max(
        (rules.degree_levels[word] for word in rules.degree_pattern.findall(fact_text)),
        default=rules.unstated_degree_max,
    )
    return [
        GroundingIssue(
            kind=IssueKind.DEGREE,
            message=f"「{word}」高于引用事实里的程度（最高到「{rules.degree_names[allowed]}」）",
            fragment=word,
        )
        for word in words
        if rules.degree_levels[word] > allowed
    ]
