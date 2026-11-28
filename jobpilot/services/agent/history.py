"""把本人对话的原文分来源返回，工具输出由网关包装为不可信数据。"""

from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from jobpilot.config import settings
from jobpilot.db.conversations import find_conversation
from jobpilot.db.history import history_rows, legacy_question_card
from jobpilot.db.models import AgentAction, Message, MessageRole
from jobpilot.services.agent.context import serialize
from jobpilot.services.agent.questions import (
    OrdinaryQuestions,
    load_question_card,
    question_results,
)
from jobpilot.services.agent.step_details import model_view
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.llm_gateway import (
    pii_at_boundary,
    pii_spans,
    preview_text,
    redact_pii,
)

MAX_HISTORY_PARTS = 10
MAX_HISTORY_BYTES = 4500
MAX_HISTORY_PART_BYTES = 1800

_OBJECT_TOOLS = {
    "job_id": "read_job",
    "report_id": "read_match_report",
    "fact_set_id": "read_facts",
}


class HistorySource(StrEnum):
    USER = "user_body"
    ATTACHMENT = "attachment"
    ASSISTANT = "assistant"
    ANSWER = "question_answer"
    TOOL = "tool_step"


class HistoryCursor(BaseModel):
    seq: int = Field(ge=1)
    part: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)


class HistoryArgs(ToolArgs):
    conversation_id: UUID | None = None
    start_seq: int | None = Field(default=None, ge=1)
    end_seq: int | None = Field(default=None, ge=1)
    query: str | None = Field(default=None, min_length=1, max_length=120)
    cursor: HistoryCursor | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        if bool(self.query) == (self.start_seq is not None or self.end_seq is not None):
            raise ValueError("请按序号范围或关键词查询")
        if self.end_seq is not None and (self.start_seq or 1) > self.end_seq:
            raise ValueError("起始序号不能大于结束序号")
        return self


@dataclass(frozen=True)
class HistoryPart:
    source: HistorySource
    text: str
    metadata: Payload | None = None
    searchable_values: tuple[str, ...] | None = None


def _text_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _text_values(child)


def _old_answers(action: AgentAction | None) -> Payload:
    if action is None or not action.decision_json:
        return {}
    card = load_question_card(action.args_json)
    return (
        question_results(card, action.decision_json)["answers"]
        if isinstance(card, OrdinaryQuestions)
        else {}
    )


def _tool_parts(
    message: Message, legacy_card: AgentAction | None
) -> Iterator[HistoryPart]:
    for step in message.steps_json:
        if "confirm" in step:
            continue
        answers = step.get("answers")
        if isinstance(answers, list):
            answers = _old_answers(legacy_card)
        for question_id, answer in answers.items() if isinstance(answers, dict) else ():
            if isinstance(answer, dict):
                yield HistoryPart(
                    HistorySource.ANSWER,
                    f"{answer.get('question', '')}\n{answer.get('answer', '')}",
                    {"question_id": question_id},
                )
        summary = {
            key: value for key, value in model_view(step).items() if key != "answers"
        }
        readers = {key: tool for key, tool in _OBJECT_TOOLS.items() if key in summary}
        if summary.get("tool") in {"tailor_resume", "read_resume", "edit_resume_item"}:
            if "run_id" in summary:
                readers["run_id"] = "read_resume"
        yield HistoryPart(
            HistorySource.TOOL,
            serialize(summary),
            readers or None,
            tuple(_text_values(summary)),
        )


def history_parts(
    message: Message, legacy_card: AgentAction | None = None
) -> Iterator[HistoryPart]:
    if message.role == MessageRole.TOOL:
        yield from _tool_parts(message, legacy_card)
    elif message.role == MessageRole.ASSISTANT:
        if message.content:
            yield HistoryPart(HistorySource.ASSISTANT, message.content)
    else:
        if message.content:
            yield HistoryPart(HistorySource.USER, message.content)
        for index, attachment in enumerate(message.attachments_json, 1):
            yield HistoryPart(
                HistorySource.ATTACHMENT,
                attachment["text"],
                {
                    "index": index,
                    "name": preview_text(attachment.get("name") or "", 24) or None,
                    "kind": attachment.get("kind"),
                },
            )


def _part_payload(
    seq: int, part: HistoryPart, offset: int, limit: int, spans: list[tuple[int, int]]
) -> tuple[Payload, int]:
    # JSON 转义也占出站字节，不能按 Python 字符数直接分页。
    left, right = 0, min(limit, len(part.text) - offset)
    while left < right:
        middle = (left + right + 1) // 2
        size = len(serialize(part.text[offset : offset + middle]).encode())
        if size > limit:
            right = middle - 1
        else:
            left = middle
    end = offset + left
    if span := pii_at_boundary(spans, end):
        start, contact_end = span
        # 极长的单个联系方式也要让下一页前进；它只占一个脱敏占位符。
        end = start if start > offset else contact_end
    row = {
        "seq": seq,
        "source": part.source,
        "text": redact_pii(part.text[offset:end]),
        "truncated": offset > 0 or end < len(part.text),
    }
    if part.metadata:
        row["metadata"] = part.metadata
    return row, end


def _page(rows: list[Payload], cursor: HistoryCursor | None) -> Payload:
    return {
        "rows": rows,
        "next_cursor": cursor.model_dump() if cursor is not None else None,
    }


def _text_offset(text: str, needle: str) -> int:
    folded = text.casefold()
    offset = folded.find(needle)
    if offset < 0 or len(text) == len(folded):
        return offset
    for index, character in enumerate(text):
        offset -= len(character.casefold())
        if offset < 0:
            return index
    return -1


def _search_offset(part: HistoryPart, needle: str) -> int:
    if part.searchable_values is None:
        return _text_offset(part.text, needle)
    for value in part.searchable_values:
        offset = _text_offset(value, needle)
        if offset >= 0:
            return (
                part.text.index(serialize(value)) + len(serialize(value[:offset])) - 1
            )
    return -1


async def read_history(context: ToolContext, args: HistoryArgs) -> Payload:
    agent = context.agent
    conversation_id = args.conversation_id or agent.conversation_id
    cursor = args.cursor or HistoryCursor(seq=args.start_seq or 1, part=0)
    rows: list[Payload] = []
    budget = min(MAX_HISTORY_BYTES, settings.agent_limits.tool_result_bytes)
    limit = min(MAX_HISTORY_PART_BYTES, budget // 3)
    needle = args.query.casefold() if args.query else None
    async with agent.sessions() as session:
        if args.conversation_id is not None and not await find_conversation(
            agent.user_id, session, conversation_id
        ):
            raise ToolError("对话不存在", 404)
        messages = history_rows(
            agent.user_id,
            session,
            conversation_id,
            start_seq=max(args.start_seq or 1, cursor.seq),
            end_seq=args.end_seq,
        )
        async with aclosing(messages):
            async for message in messages:
                legacy_card = (
                    await legacy_question_card(session, message)
                    if any(
                        isinstance(step.get("answers"), list)
                        for step in message.steps_json
                    )
                    else None
                )
                for index, part in enumerate(history_parts(message, legacy_card)):
                    if message.seq == cursor.seq and index < cursor.part:
                        continue
                    offset = (
                        cursor.offset
                        if (message.seq, index) == (cursor.seq, cursor.part)
                        else 0
                    )
                    if needle:
                        found = _search_offset(part, needle)
                        if found < 0:
                            continue
                        offset = max(offset, found - limit // 12)
                    spans = pii_spans(part.text)
                    if span := pii_at_boundary(spans, offset):
                        offset = span[0]
                    current = HistoryCursor(seq=message.seq, part=index, offset=offset)
                    row, end = _part_payload(message.seq, part, offset, limit, spans)
                    next_chunk = HistoryCursor(seq=message.seq, part=index, offset=end)
                    next_part = HistoryCursor(seq=message.seq, part=index + 1)
                    following = next_chunk if end < len(part.text) else next_part
                    if (
                        len(rows) == MAX_HISTORY_PARTS
                        or len(serialize(_page([*rows, row], following)).encode())
                        > budget
                    ):
                        return _page(rows, current)
                    rows.append(row)
                    if end < len(part.text):
                        return _page(rows, following)
    return _page(rows, None)
