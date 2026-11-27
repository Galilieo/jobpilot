"""把当前对话的原文分来源返回，工具输出继续由网关包装为不可信数据。"""

from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from jobpilot.config import settings
from jobpilot.db.history import history_rows
from jobpilot.db.models import Message, MessageRole
from jobpilot.services.agent.context import serialize
from jobpilot.services.agent.step_details import model_view
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext

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


def _tool_parts(message: Message) -> Iterator[HistoryPart]:
    for step in message.steps_json:
        if "confirm" in step:
            continue
        answers = step.get("answers")
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
        yield HistoryPart(HistorySource.TOOL, serialize(summary), readers or None)


def history_parts(message: Message) -> Iterator[HistoryPart]:
    if message.role == MessageRole.TOOL:
        yield from _tool_parts(message)
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
                    "name": (attachment.get("name") or "")[:24] or None,
                    "kind": attachment.get("kind"),
                },
            )


def _part_payload(
    seq: int, part: HistoryPart, offset: int, limit: int
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
    row = {
        "seq": seq,
        "source": part.source,
        "text": part.text[offset:end],
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


async def read_history(context: ToolContext, args: HistoryArgs) -> Payload:
    agent = context.agent
    cursor = args.cursor or HistoryCursor(seq=args.start_seq or 1, part=0)
    rows: list[Payload] = []
    budget = min(MAX_HISTORY_BYTES, settings.agent_limits.tool_result_bytes)
    limit = min(MAX_HISTORY_PART_BYTES, budget // 3)
    async with agent.sessions() as session:
        messages = history_rows(
            agent.user_id,
            session,
            agent.conversation_id,
            start_seq=max(args.start_seq or 1, cursor.seq),
            end_seq=args.end_seq,
            query=args.query,
        )
        async with aclosing(messages):
            async for message in messages:
                for index, part in enumerate(history_parts(message)):
                    if message.seq == cursor.seq and index < cursor.part:
                        continue
                    offset = (
                        cursor.offset
                        if (message.seq, index) == (cursor.seq, cursor.part)
                        else 0
                    )
                    if args.query:
                        found = part.text.casefold().find(args.query.casefold())
                        if found < 0:
                            continue
                        offset = max(offset, found - limit // 12)
                    current = HistoryCursor(seq=message.seq, part=index, offset=offset)
                    row, end = _part_payload(message.seq, part, offset, limit)
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
