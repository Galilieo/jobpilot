"""核实压缩输出引用的是本人正文或已经提交的提问卡答案。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.compactions import (
    cited_question_cards,
    cited_user_bodies,
    executed_cards,
    submitted_questions,
)
from jobpilot.db.models import AgentAction
from jobpilot.services.agent.questions import (
    OrdinaryQuestions,
    load_question_card,
    question_results,
)
from jobpilot.services.agent.work_summary import (
    MAX_ITEMS,
    AnsweredQuestion,
    ExecutedAction,
    WorkSummary,
)

type QuestionIndex = dict[tuple[UUID, str], AnsweredQuestion]

_ACTION_RESULT_FIELDS = (
    "application_id",
    "conversation_id",
    "decision",
    "job_id",
    "key",
    "op",
    "revision",
    "run_id",
    "version_id",
)


async def verified_actions(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, through_seq: int
) -> list[ExecutedAction]:
    cards = await executed_cards(
        user_id, session, conversation_id, through_seq=through_seq, limit=MAX_ITEMS
    )
    return [
        ExecutedAction(
            action_id=card.action_id,
            tool=card.tool,
            status=card.status,
            result={
                key: card.result_json[key]
                for key in _ACTION_RESULT_FIELDS
                if key in card.result_json
            },
        )
        for card in cards
        if card.result_json is not None
    ]


def _answers(action: AgentAction) -> list[AnsweredQuestion]:
    card = load_question_card(action.args_json)
    if not isinstance(card, OrdinaryQuestions) or not action.decision_json:
        return []
    return [
        AnsweredQuestion(action_id=action.action_id, question_id=key, **result)
        for key, result in question_results(card, action.decision_json)[
            "answers"
        ].items()
    ]


async def batch_answers(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, run_ids: set[UUID]
) -> dict[UUID, list[AnsweredQuestion]]:
    by_run: dict[UUID, list[AnsweredQuestion]] = {}
    for card in await submitted_questions(
        user_id, session, conversation_id, run_ids=run_ids
    ):
        by_run.setdefault(card.run_id, []).extend(_answers(card))
    return by_run


async def verified_sources(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    summary: WorkSummary,
    new_questions: list[AnsweredQuestion],
    through_seq: int,
) -> tuple[dict[int, str], QuestionIndex]:
    sequences = {
        item.source.message_seq
        for item in summary.requirements
        if item.source.message_seq is not None
    }
    action_ids = {q.action_id for q in [*summary.answered_questions, *new_questions]}
    action_ids.update(
        item.source.action_id
        for item in summary.requirements
        if item.source.action_id is not None
    )
    bodies = await cited_user_bodies(
        user_id, session, conversation_id, sequences=sequences, through_seq=through_seq
    )
    cards = await cited_question_cards(
        user_id,
        session,
        conversation_id,
        action_ids=action_ids,
        through_seq=through_seq,
    )
    answers = {
        (answer.action_id, answer.question_id): answer
        for card in cards
        for answer in _answers(card)
    }
    return bodies, answers
