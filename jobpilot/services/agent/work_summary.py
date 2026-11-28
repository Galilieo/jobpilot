"""校验和渲染有来源的滚动工作摘要。"""

from collections.abc import Mapping
from json import dumps
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from jobpilot.services.llm_gateway import preview_text, wrap_untrusted_text
from jobpilot.services.text_clauses import contains_complete_sentence

MAX_ITEMS = 12
MAX_REQUIREMENTS = 24
MAX_ITEM_CHARACTERS = 240
MAX_QUOTE_CHARACTERS = 100
MAX_QUESTION_CHARACTERS = 2000
_LOOKUP_HINT = "可以用 read_conversation_history 按消息序号或关键词查原文。"

SummaryText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_ITEM_CHARACTERS
    ),
]
SourceQuote = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_QUOTE_CHARACTERS)
]
QuestionText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARACTERS)
]


class SummaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(SummaryModel):
    message_seq: int | None = Field(default=None, gt=0)
    action_id: UUID | None = None
    question_id: str | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def one_source(self) -> "Source":
        if self.message_seq is not None:
            if self.action_id is None and self.question_id is None:
                return self
        elif self.action_id is not None and self.question_id is not None:
            return self
        raise ValueError("必须提供消息序号，或确认卡和问题 ID")


class Requirement(SummaryModel):
    id: str = Field(min_length=1, max_length=40)
    statement: SummaryText
    quote: SourceQuote
    source: Source
    job_id: UUID | None = None
    supersedes: str | None = Field(default=None, max_length=40)


class AnsweredQuestion(SummaryModel):
    action_id: UUID
    question_id: str = Field(min_length=1, max_length=20)
    question: QuestionText
    answer: QuestionText


class WorkSummary(SummaryModel):
    goal: SummaryText | None = None
    requirements: list[Requirement] = Field(
        default_factory=list, max_length=MAX_REQUIREMENTS
    )
    done_and_decided: list[SummaryText] = Field(
        default_factory=list, max_length=MAX_ITEMS
    )
    open_items: list[SummaryText] = Field(default_factory=list, max_length=MAX_ITEMS)
    answered_questions: list[AnsweredQuestion] = Field(
        default_factory=list, max_length=MAX_ITEMS
    )
    unverified: list[SummaryText] = Field(default_factory=list, max_length=MAX_ITEMS)


class ExecutedAction(SummaryModel):
    action_id: UUID
    tool: str
    status: str
    result: dict[str, str | int | None]


class StoredWorkSummary(WorkSummary):
    # 旧摘要只做过子串校验，重新核对前不能把旧引文当成完整原话。
    quotes_verified: bool = False
    executed_actions: list[ExecutedAction] = Field(
        default_factory=list, max_length=MAX_ITEMS
    )


def contains_complete_quote(body: str, quote: str) -> bool:
    """逐字核对一条或相邻几条完整句子，保留含限定语的整句。"""
    return contains_complete_sentence(body, quote)


def _has_quote(
    requirement: Requirement,
    bodies: Mapping[int, str],
    answers: Mapping[tuple[UUID, str], AnsweredQuestion],
) -> bool:
    source = requirement.source
    if source.message_seq is not None:
        return contains_complete_quote(
            bodies.get(source.message_seq, ""), requirement.quote
        )
    if source.action_id is not None and source.question_id:
        question = answers.get((source.action_id, source.question_id))
        return question is not None and contains_complete_quote(
            question.answer, requirement.quote
        )
    return False


def _range_hint(through_seq: int) -> str:
    return f"消息 1～{through_seq} 里还有相关内容，请按需查原文。"


def checked_summary(
    summary: WorkSummary,
    bodies: Mapping[int, str],
    answers: Mapping[tuple[UUID, str], AnsweredQuestion],
    *,
    through_seq: int,
    executed_actions: list[ExecutedAction] | None = None,
) -> StoredWorkSummary:
    verified = []
    unverified = list(summary.unverified)
    for requirement in summary.requirements:
        if _has_quote(requirement, bodies, answers):
            verified.append(requirement)
        else:
            unverified.append(requirement.statement)
    for question in summary.answered_questions:
        stored = answers.get((question.action_id, question.question_id))
        if stored != question:
            preview = preview_text(question.question, MAX_ITEM_CHARACTERS).strip()
            if preview:
                unverified.append(preview)
    confirmed = list(answers.values())
    if (
        len(unverified) > MAX_ITEMS
        or len(confirmed) > MAX_ITEMS
        or len(verified) > MAX_REQUIREMENTS
    ):
        unverified = [*unverified[: MAX_ITEMS - 1], _range_hint(through_seq)]
    return StoredWorkSummary(
        goal=summary.goal,
        requirements=verified[-MAX_REQUIREMENTS:],
        done_and_decided=summary.done_and_decided,
        open_items=summary.open_items,
        answered_questions=confirmed[-MAX_ITEMS:],
        unverified=unverified,
        quotes_verified=True,
        executed_actions=(executed_actions or [])[-MAX_ITEMS:],
    )


def render_summary(summary: StoredWorkSummary) -> str:
    headings: list[tuple[str, list[str]]] = [
        ("目标（模型概括，未核实）", [summary.goal] if summary.goal else []),
        (
            "要求" if summary.quotes_verified else "旧摘要引文（未核实）",
            [item.quote.strip() for item in summary.requirements],
        ),
        ("已完成和已决定（模型概括，未核实）", summary.done_and_decided),
        (
            "已确认的动作结果",
            [action.model_dump_json() for action in summary.executed_actions],
        ),
        ("待处理", summary.open_items),
        (
            "已回答的问题",
            [question.model_dump_json() for question in summary.answered_questions],
        ),
        ("待核实", summary.unverified),
    ]
    blocks = [
        heading + "\n" + "\n".join(f"- {item}" for item in items)
        for heading, items in headings
        if items
    ]
    return "\n".join([*blocks, _LOOKUP_HINT])


def _summary_bytes(summary: StoredWorkSummary) -> int:
    wrapped = wrap_untrusted_text("work_summary", render_summary(summary))
    return len(dumps(wrapped, ensure_ascii=False).encode())


def fit_summary(
    summary: StoredWorkSummary, maximum_bytes: int, through_seq: int
) -> StoredWorkSummary:
    """空间不足时保留范围提示，优先保留已经验证的要求。"""
    if _summary_bytes(summary) <= maximum_bytes:
        return summary
    reduced = summary.model_copy(deep=True)
    fields = (
        reduced.unverified,
        reduced.done_and_decided,
        reduced.open_items,
        reduced.executed_actions,
        reduced.answered_questions,
        reduced.requirements,
    )
    for field in fields:
        if field is reduced.unverified:
            del reduced.unverified[MAX_ITEMS - 1 :]
        while field and _summary_bytes(reduced) > maximum_bytes:
            field.pop(0)
        if field is reduced.unverified:
            reduced.unverified.append(_range_hint(through_seq))
        if _summary_bytes(reduced) <= maximum_bytes:
            return reduced
    return StoredWorkSummary(unverified=[_range_hint(through_seq)])
