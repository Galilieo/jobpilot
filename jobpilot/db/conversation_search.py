"""在本人其他对话范围内取回向量、全文命中和原文出处。"""

from dataclasses import dataclass, fields
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.dialects.postgresql import TSQUERY
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
    content: str
    source_text: str
    span_start: int
    span_end: int
    score: float


_RESULT_FIELDS = [field.name for field in fields(RankedChunk)]


def _candidates(scope: SearchScope) -> Select:
    return (
        select(
            MessageChunk.chunk_id,
            MessageChunk.conversation_id,
            Conversation.title,
            Message.seq,
            Message.role,
            Message.created_at.label("date"),
            MessageChunk.content,
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


async def has_pending_embeddings(
    scope: SearchScope, session: AsyncSession, model: str
) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    MessageChunk.user_id == scope.user_id,
                    MessageChunk.conversation_id != scope.current_conversation_id,
                    MessageChunk.embedding_model.is_distinct_from(model),
                )
            )
        )
    )


async def vector_candidates(
    scope: SearchScope,
    session: AsyncSession,
    model: str,
    vector: list[float],
    limit: int,
) -> list[RankedChunk]:
    score = 1 - MessageChunk.embedding.cosine_distance(vector)
    statement = _candidates(scope).where(MessageChunk.embedding_model == model)
    return await _top_distinct(session, statement, score, limit)


async def keyword_candidates(
    scope: SearchScope,
    session: AsyncSession,
    terms: list[str],
    limit: int,
    *,
    missing_model: str | None = None,
) -> list[RankedChunk]:
    if not terms:
        return []
    # 每个词作为普通文本解析，OR 只由程序组装。
    first, *rest = terms
    query = func.plainto_tsquery("simple", first)
    for term in rest:
        query = query.op("||", return_type=TSQUERY())(
            func.plainto_tsquery("simple", term)
        ).self_group()
    score = func.ts_rank_cd(MessageChunk.search_terms, query)
    statement = _candidates(scope).where(MessageChunk.search_terms.op("@@")(query))
    if missing_model is not None:
        statement = statement.where(
            MessageChunk.embedding_model.is_distinct_from(missing_model)
        )
    return await _top_distinct(session, statement, score, limit)


async def _top_distinct(
    session: AsyncSession, candidates: Select, score: ColumnElement[float], limit: int
) -> list[RankedChunk]:
    """按分数取前几条，同一段文字只留排名最前的一份。

    按钮发出的同一句话会出现在很多段对话里，短句又和短查询很接近，
    不合并就会占满结果（2026-11-28 实际使用中前 5 条是同一句）。
    """
    best_first = (score.desc(), Message.seq.desc(), MessageChunk.chunk_no)
    ranked = candidates.add_columns(
        score.label("score"),
        MessageChunk.chunk_no,
        func.row_number()
        .over(partition_by=MessageChunk.content, order_by=best_first)
        .label("copy_no"),
    ).subquery()
    statement = (
        select(*(ranked.c[name] for name in _RESULT_FIELDS))
        .where(ranked.c.copy_no == 1)
        .order_by(ranked.c.score.desc(), ranked.c.seq.desc(), ranked.c.chunk_no)
        .limit(limit)
    )
    return [RankedChunk(**row) for row in (await session.execute(statement)).mappings()]
