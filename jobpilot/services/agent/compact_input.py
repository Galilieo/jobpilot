"""把已结束的连续轮次装入压缩请求，在输入预算内推进最早边界。"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from json import dumps
from uuid import UUID

from openai.types.chat import ChatCompletionMessageParam

from jobpilot.db.models import Message
from jobpilot.services.agent.context import serialize
from jobpilot.services.agent.history import HistorySource, history_parts
from jobpilot.services.agent.work_summary import AnsweredQuestion, WorkSummary
from jobpilot.services.llm_gateway import (
    preview_text,
    redact_pii,
    request_messages_for_model,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt

MAX_ATTACHMENT_PREVIEW = 500
MAX_TOOL_PREVIEW = 2000


@dataclass(frozen=True)
class CompletedTurn:
    run_id: UUID
    messages: list[Message]

    @property
    def end_seq(self) -> int:
        return self.messages[-1].seq


def group_turns(messages: list[Message]) -> list[CompletedTurn]:
    return [
        CompletedTurn(run_id, list(rows))
        for run_id, rows in groupby(messages, key=lambda row: row.run_id)
    ]


def _turn_rows(turn: CompletedTurn, *, preview_chars: int | None = None) -> list[dict]:
    rows = []
    for message in turn.messages:
        for part in history_parts(message):
            limit = {
                HistorySource.ATTACHMENT: MAX_ATTACHMENT_PREVIEW,
                HistorySource.TOOL: MAX_TOOL_PREVIEW,
            }.get(part.source)
            if preview_chars is not None:
                limit = (
                    0
                    if part.source in {HistorySource.ATTACHMENT, HistorySource.TOOL}
                    else preview_chars
                )
            row = {
                "seq": message.seq,
                "source": part.source,
            }
            if part.metadata:
                row["metadata"] = part.metadata
            if limit is None or len(part.text) <= limit:
                row["text"] = redact_pii(part.text)
            else:
                row["text"] = preview_text(part.text, limit)
                row["reference"] = f"原文见消息 {message.seq}"
            rows.append(row)
    return rows


def _request(
    previous: WorkSummary,
    rows: list[dict],
    answers: list[AnsweredQuestion],
    focus: str | None,
) -> list[ChatCompletionMessageParam]:
    content = [
        wrap_untrusted_text(
            "previous_summary",
            previous.model_dump_json(
                exclude_none=True, exclude={"quotes_verified", "executed_actions"}
            ),
        ),
        wrap_untrusted_text("completed_turns", serialize(rows)),
    ]
    if answers:
        content.append(
            wrap_untrusted_text(
                "submitted_questions",
                serialize([answer.model_dump(mode="json") for answer in answers]),
            )
        )
    if focus:
        content.append(wrap_untrusted_text("focus", focus))
    return [
        {"role": "system", "content": load_prompt("compact_conversation").text},
        {"role": "user", "content": "\n\n".join(content)},
    ]


def _size(messages: list[ChatCompletionMessageParam]) -> int:
    return len(dumps(request_messages_for_model(messages), ensure_ascii=False).encode())


@dataclass(frozen=True)
class CompactBatch:
    turns: list[CompletedTurn]
    messages: list[ChatCompletionMessageParam]

    @property
    def input_size(self) -> int:
        return _size(self.messages)


def select_batch(
    previous: WorkSummary,
    candidates: Sequence[CompletedTurn],
    answers_by_run: dict[UUID, list[AnsweredQuestion]],
    *,
    focus: str | None,
    input_limit: int,
) -> CompactBatch | None:
    rows: list[dict] = []
    answers: list[AnsweredQuestion] = []
    accepted: list[CompletedTurn] = []
    accepted_request: list[ChatCompletionMessageParam] = []
    for turn in candidates:
        new_rows = _turn_rows(turn)
        new_answers = answers_by_run.get(turn.run_id, [])
        request = _request(
            previous, [*rows, *new_rows], [*answers, *new_answers], focus
        )
        if _size(request) > input_limit:
            if accepted:
                break
            return _bounded_first(previous, turn, focus, input_limit)
        rows.extend(new_rows)
        answers.extend(new_answers)
        accepted.append(turn)
        accepted_request = request
    if not accepted:
        return None
    return CompactBatch(accepted, accepted_request)


def _bounded_first(
    previous: WorkSummary,
    turn: CompletedTurn,
    focus: str | None,
    input_limit: int,
) -> CompactBatch | None:
    """单轮过大时也推进完整轮次，最小输入保留可找回的消息范围。"""
    for characters in (1000, 250, 0):
        request = _request(
            previous, _turn_rows(turn, preview_chars=characters), [], focus
        )
        if _size(request) <= input_limit:
            return CompactBatch([turn], request)
    request = _request(
        previous,
        [
            {
                "reference": f"原文见消息 {turn.messages[0].seq}～{turn.end_seq}",
            }
        ],
        [],
        focus,
    )
    return CompactBatch([turn], request) if _size(request) <= input_limit else None
