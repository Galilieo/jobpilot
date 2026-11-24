"""简历事实的拆分与原文校验。

模型只给出原文片段的文字，位置由程序在用户核对后的 raw_text 里查找：
模型数不准字符位置，网关外发前的脱敏占位符也会改变文本长度。

原文校验只能证明片段真实存在，证明不了模型转述的 text 和标签忠于片段。
所以另用程序比对数字和角色词，对不上的事实标记出来，由用户逐条核对。
"""

import re
import unicodedata
from enum import StrEnum
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.db.models import FactKind, Responsibility
from jobpilot.services.grounding_rules import RESPONSIBILITY_RANK, default_rules
from jobpilot.services.llm_gateway import (
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt

Span = tuple[int, int]  # (start, end)，满足 raw_text[start:end] 是原文中的那一段


def locate_quote(
    raw_text: str, quote: str, start: int = 0, end: int | None = None
) -> list[Span]:
    """在 raw_text[start:end] 范围内查找 quote 的全部出现位置，比较时忽略空白。

    其他字符必须完全一致：数字、全角半角、繁简不同都算对不上。
    返回的位置首尾都不是空白，中间的换行和空格原样保留。
    """
    if end is None:
        end = len(raw_text)

    needle = "".join(char for char in quote if not char.isspace())
    if not needle:
        # 空字符串在任何位置都能「找到」，必须当作找不到，否则会被误判为校验通过。
        return []

    # 去掉空白后的文本，以及其中每个字符在原文中的位置。
    positions = [i for i in range(start, end) if not raw_text[i].isspace()]
    haystack = "".join(raw_text[i] for i in positions)

    spans: list[Span] = []
    found = haystack.find(needle)
    while found != -1:
        last = found + len(needle) - 1
        spans.append((positions[found], positions[last] + 1))
        found = haystack.find(needle, found + 1)

    return spans


# ---------- 模型输出：按经历分组的事实 ----------


class SectionKind(StrEnum):
    """简历的段落类型，对应 01 F1 的六类。"""

    EDUCATION = "education"
    PROJECT = "project"
    INTERNSHIP = "internship"
    SKILL = "skill"
    AWARD = "award"
    OTHER = "other"


# 只有项目和实习区分职责程度，其他段落的事实一律为 na。
RESPONSIBILITY_SECTIONS = frozenset({SectionKind.PROJECT, SectionKind.INTERNSHIP})


class _ModelOutput(BaseModel):
    """模型输出多出字段时校验失败，触发网关的 JSON 修复。"""

    model_config = ConfigDict(extra="forbid")


class ExtractedFact(_ModelOutput):
    kind: FactKind
    responsibility: Responsibility
    responsibility_stated: bool
    text: str = Field(min_length=1, max_length=300)
    has_number: bool
    source_quote: str = Field(min_length=1, max_length=1000)


class ExtractedExperience(_ModelOutput):
    name: str = Field(min_length=1, max_length=200)
    section: SectionKind
    heading_quote: str = Field(max_length=300)
    facts: list[ExtractedFact] = Field(max_length=60)


class FactExtraction(_ModelOutput):
    experiences: list[ExtractedExperience] = Field(max_length=40)


class TruncatedOutputError(RuntimeError):
    """模型输出因长度上限被截断。重发同样会截断，所以不走 JSON 修复，直接报错。"""


def parse_extraction(completion: ChatCompletion) -> FactExtraction:
    """校验并解析模型输出；作为网关的 validate 回调时，返回值不会被使用。"""
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError("事实拆分的输出超过长度上限，被截断")
    return FactExtraction.model_validate_json(choice.message.content or "")


# ---------- 拆分结果：草稿事实与分段 ----------


class ReviewReason(StrEnum):
    """需要用户核对的原因。只有 QUOTE_NOT_FOUND 阻止确认，其余要求逐条看过。"""

    QUOTE_NOT_FOUND = "quote_not_found"
    EXPERIENCE_UNCERTAIN = "experience_uncertain"
    QUOTE_AMBIGUOUS = "quote_ambiguous"
    RESPONSIBILITY_UNSTATED = "responsibility_unstated"
    RESPONSIBILITY_UNSUPPORTED = "responsibility_unsupported"
    NUMBER_NOT_IN_QUOTE = "number_not_in_quote"


class Section(BaseModel):
    """一段经历在 raw_text 中的范围 [start, end)，存入 sections_json。"""

    name: str
    section: SectionKind
    start: int
    end: int


class DraftFact(BaseModel):
    """草稿中的一条事实，存入 fact_draft.items_json，等用户确认。

    校验通过时 source_quote 是 raw_text[span_start:span_end]；
    未通过时保留模型给出的文字供用户对照，位置为空。
    """

    experience: str
    section: SectionKind
    kind: FactKind
    responsibility: Responsibility
    text: str
    has_number: bool
    source_doc_id: UUID
    source_quote: str
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    sections: list[Section]
    facts: list[DraftFact]


# ---------- 转述检查：text 与标签是否有片段支撑 ----------

# 职责词及其级别放在 jobpilot/data/grounding_rules.yaml，和依据检查共用。
# 参与级的词不支撑任何职责，只用来截断前面职责词的作用范围。
# 结果类事实往前查找职责词时，不越过句末标点和换行。
_SENTENCE_BREAK = re.compile(r"[。！？；!?;\n]")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def role_context(raw_text: str, start: int, end: int, kind: FactKind) -> str:
    """返回检查职责时要看的文字：从职责词的作用范围起点到片段结尾。

    做法和细节沿用同一行前面的职责词，从行首算起，可以越过句号；
    结果最容易被夸大，只认结果所在的那一句，从片段结尾所在句子的句首算起。
    """
    if kind is FactKind.RESULT:
        breaks = _SENTENCE_BREAK.finditer(raw_text, 0, max(end - 1, 0))
        scope_start = max((match.end() for match in breaks), default=0)
    else:
        scope_start = raw_text.rfind("\n", 0, start) + 1
    return raw_text[scope_start:end]


def responsibility_supported(responsibility: Responsibility, context: str) -> bool:
    """lead、own 要求 context 中最后出现的职责词达到对应级别；participate、na 不检查。

    取最后一个词：「负责接口开发，参与压测」中，压测归「参与」管，不能借用前面的「负责」。
    只看词是否出现，不理解语义：「不负责」也会被当成有「负责」。
    """
    if responsibility not in (Responsibility.LEAD, Responsibility.OWN):
        return True
    rules = default_rules()
    words = rules.role_pattern.findall(context)
    return bool(words) and (
        RESPONSIBILITY_RANK[rules.role_levels[words[-1]]]
        >= RESPONSIBILITY_RANK[responsibility]
    )


def numbers_missing_from_quote(text: str, quote: str) -> list[str]:
    """返回 text 中出现、片段中却没有的数字。

    按完整的数字比较，不按子串：text 的「5」不能靠片段里的「50」通过。
    全角数字先转成半角。中文数字（三成、十二个）不在检查范围内。
    """
    quote_numbers = set(_numbers(quote))
    return [number for number in _numbers(text) if number not in quote_numbers]


def contains_number(text: str) -> bool:
    """text 中有阿拉伯数字（含全角）。中文数字由模型的 has_number 判断。"""
    return bool(_numbers(text))


def _numbers(text: str) -> list[str]:
    return _NUMBER_PATTERN.findall(unicodedata.normalize("NFKC", text))


# ---------- 拆分流程 ----------
async def extract_facts(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    budget: ModelBudget,
    doc_id: UUID,
    raw_text: str,
) -> ExtractionResult:
    """调用模型把简历拆成事实，再由程序分段、定位原文并检查转述。

    raw_text 是用户核对后保存的原文；调用前需已提交所属 ai_run。
    """
    prompt = load_prompt("extract_facts")
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": prompt.text},
        {
            "role": "user",
            "content": wrap_untrusted_text("resume", raw_text),
        },
    ]
    completion = await request_completion(
        session_factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.EXTRACT_FACTS,
        prompt_version=prompt.label,
        messages=messages,
        json_output=True,
        budget=budget,
        validate=parse_extraction,
    )
    extraction = parse_extraction(completion)

    sections = locate_sections(raw_text, extraction.experiences)
    facts = [
        _to_draft(fact, experience, section, raw_text, doc_id)
        for experience, section in zip(extraction.experiences, sections, strict=True)
        for fact in experience.facts
    ]
    located = sorted((s for s in sections if s is not None), key=lambda s: s.start)
    return ExtractionResult(sections=located, facts=facts)


def locate_sections(
    raw_text: str, experiences: list[ExtractedExperience]
) -> list[Section | None]:
    """按标题位置把原文切成段，每段到下一个标题之前为止。

    返回值与 experiences 一一对应；标题在原文中找不到的经历为 None。
    """
    starts: list[int | None] = []
    for experience in experiences:
        spans = locate_quote(raw_text, experience.heading_quote)
        starts.append(spans[0][0] if spans else None)

    boundaries = sorted({start for start in starts if start is not None})
    sections: list[Section | None] = []
    for experience, start in zip(experiences, starts, strict=True):
        if start is None:
            sections.append(None)
            continue
        later = [b for b in boundaries if b > start]
        end = later[0] if later else len(raw_text)
        sections.append(
            Section(
                name=experience.name, section=experience.section, start=start, end=end
            )
        )
    return sections


def _to_draft(
    fact: ExtractedFact,
    experience: ExtractedExperience,
    section: Section | None,
    raw_text: str,
    doc_id: UUID,
) -> DraftFact:
    """定位原文片段，再用程序规则校正职责、检查转述，记下需要用户核对的原因。"""
    reasons: list[ReviewReason] = []

    # 有范围就只在本段找：在别的段找到，说明归属可能错了，不能算通过。
    if section is None:
        spans = locate_quote(raw_text, fact.source_quote)
        if spans:
            reasons.append(ReviewReason.EXPERIENCE_UNCERTAIN)
    else:
        spans = locate_quote(raw_text, fact.source_quote, section.start, section.end)

    if spans:
        span_start, span_end = spans[0]
        quote = raw_text[span_start:span_end]
        if len(spans) > 1:
            reasons.append(ReviewReason.QUOTE_AMBIGUOUS)
    else:
        span_start = span_end = None
        quote = fact.source_quote
        reasons.append(ReviewReason.QUOTE_NOT_FOUND)

    responsibility = fact.responsibility
    if experience.section not in RESPONSIBILITY_SECTIONS:
        responsibility = Responsibility.NA

    elif not fact.responsibility_stated or responsibility is Responsibility.NA:
        responsibility = Responsibility.PARTICIPATE
        reasons.append(ReviewReason.RESPONSIBILITY_UNSTATED)

    # 下面两项比对的是转述与片段；片段没通过校验时比对没有意义，这条本来就要核对
    elif spans and not responsibility_supported(
        responsibility, role_context(raw_text, span_start, span_end, fact.kind)
    ):
        reasons.append(ReviewReason.RESPONSIBILITY_UNSUPPORTED)

    if spans and numbers_missing_from_quote(fact.text, quote):
        reasons.append(ReviewReason.NUMBER_NOT_IN_QUOTE)

    return DraftFact(
        experience=experience.name,
        section=experience.section,
        kind=fact.kind,
        responsibility=responsibility,
        text=fact.text,
        has_number=fact.has_number or contains_number(fact.text),
        source_doc_id=doc_id,
        source_quote=quote,
        span_start=span_start,
        span_end=span_end,
        quote_verified=bool(spans),
        review_reasons=reasons,
    )
