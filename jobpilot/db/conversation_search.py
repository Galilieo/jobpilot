"""在本人其他对话范围内取回向量、全文命中和原文出处。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import Conversation, Message, MessageChunk


@dataclass(frozen=True)
class SearchScope:
    user_id: UUID
    current_conversation_id: UUID


@dataclass(frozen=True)
class RankedChunk:
    chunk_id: UUID
    conversation_id: UUID
    title: str
    seq: int
    role: str
    date: datetime
    source_text: str
    span_start: int
    span_end: int
    score: float


def _candidates(scope: SearchScope) -> Select:
    return (
        select(
            MessageChunk.chunk_id,
            MessageChunk.conversation_id,
            Conversation.title,
            Message.seq,
            Message.role,
            Message.created_at.label("date"),
            Message.content.label("source_text"),
            MessageChunk.span_start,
            MessageChunk.span_end,
        )
        .join(Message, Message.message_id == MessageChunk.message_id)
        .join(
            Conversation, Conversation.conversation_id == MessageChunk.conversation_id
        )
        .where(
            MessageChunk.user_id == scope.user_id,
            MessageChunk.conversation_id != scope.current_conversation_id,
        )
    )


async def vector_candidates(
    scope: SearchScope,
    session: AsyncSession,
    model: str,
    vector: list[float],
    limit: int,
) -> list[RankedChunk]:
    distance = MessageChunk.embedding.cosine_distance(vector)
    statement = (
        _candidates(scope)
        .add_columns((1 - distance).label("score"))
        .where(MessageChunk.embedding_model == model)
        .order_by(distance, Message.seq.desc(), MessageChunk.chunk_no)
        .limit(limit)
    )
    return [RankedChunk(**row) for row in (await session.execute(statement)).mappings()]


async def keyword_candidates(
    scope: SearchScope,
    session: AsyncSession,
    terms: str,
    limit: int,
    *,
    missing_model: str | None = None,
) -> list[RankedChunk]:
    query = func.websearch_to_tsquery("simple", terms)
    score = func.ts_rank(MessageChunk.search_terms, query)
    statement = (
        _candidates(scope)
        .add_columns(score.label("score"))
        .where(MessageChunk.search_terms.op("@@")(query))
        .order_by(score.desc(), Message.seq.desc(), MessageChunk.chunk_no)
        .limit(limit)
    )
    if missing_model is not None:
        statement = statement.where(
            MessageChunk.embedding_model.is_distinct_from(missing_model)
        )
    return [RankedChunk(**row) for row in (await session.execute(statement)).mappings()]
