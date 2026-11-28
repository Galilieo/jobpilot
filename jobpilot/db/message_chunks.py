"""读取可索引的消息、保存原文跨度并按模型补全派生向量。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from jobpilot.db.models import AiRun, Message, MessageChunk, MessageRole

MIN_MESSAGE_CHARS = 4


@dataclass(frozen=True)
class ChunkSpan:
    start: int
    end: int


@dataclass(frozen=True)
class IndexChunk:
    span: ChunkSpan
    text: str
    terms: str


_INDEXABLE_MESSAGES = (
    select(Message)
    .options(
        load_only(
            Message.message_id,
            Message.user_id,
            Message.conversation_id,
            Message.seq,
            Message.content,
        )
    )
    .where(
        or_(
            Message.role == MessageRole.USER,
            (Message.role == MessageRole.ASSISTANT) & (Message.event_key == "final"),
        ),
        func.char_length(Message.content) >= MIN_MESSAGE_CHARS,
        ~exists().where(MessageChunk.message_id == Message.message_id),
    )
)


async def run_messages_to_index(
    user_id: UUID, session: AsyncSession, run_id: UUID
) -> list[Message]:
    return list(
        await session.scalars(
            _INDEXABLE_MESSAGES.join(AiRun, AiRun.run_id == Message.run_id)
            .where(
                Message.user_id == user_id,
                Message.run_id == run_id,
                AiRun.finished_at.is_not(None),
            )
            .with_for_update(of=Message, read=True, key_share=True)
        )
    )


async def message_page_to_index(
    session: AsyncSession, *, after_seq: int, limit: int
) -> list[Message]:
    """离线维护扫描所有账号，只取已经结束的轮次并锁定该批原文。"""
    return list(
        await session.scalars(
            _INDEXABLE_MESSAGES.join(AiRun, AiRun.run_id == Message.run_id)
            .where(AiRun.finished_at.is_not(None), Message.seq > after_seq)
            .order_by(Message.seq)
            .limit(limit)
            .with_for_update(of=Message, read=True, key_share=True)
        )
    )


async def insert_message_chunks(
    session: AsyncSession, message: Message, chunks: list[IndexChunk]
) -> int:
    if not chunks:
        return 0
    statement = (
        insert(MessageChunk)
        .values(
            [
                {
                    "message_id": message.message_id,
                    "conversation_id": message.conversation_id,
                    "user_id": message.user_id,
                    "chunk_no": number,
                    "span_start": chunk.span.start,
                    "span_end": chunk.span.end,
                    "content": chunk.text,
                    "search_terms": func.to_tsvector("simple", chunk.terms),
                }
                for number, chunk in enumerate(chunks)
            ]
        )
        .on_conflict_do_nothing(constraint="uq_message_chunk_number")
        .returning(MessageChunk.chunk_id)
    )
    return len((await session.scalars(statement)).all())


async def chunks_without_search_terms(
    session: AsyncSession, *, limit: int
) -> list[tuple[UUID, str]]:
    rows = await session.execute(
        select(MessageChunk.chunk_id, MessageChunk.content)
        .where(MessageChunk.search_terms.is_(None))
        .order_by(MessageChunk.chunk_id)
        .limit(limit)
    )
    return [(row.chunk_id, row.content) for row in rows]


async def store_search_terms(session: AsyncSession, chunk_id: UUID, terms: str) -> None:
    await session.execute(
        update(MessageChunk)
        .where(MessageChunk.chunk_id == chunk_id, MessageChunk.search_terms.is_(None))
        .values(search_terms=func.to_tsvector("simple", terms))
    )


def needs_embedding(model: str) -> ColumnElement[bool]:
    return MessageChunk.embedding_model.is_distinct_from(model)


async def pending_chunks(
    session: AsyncSession,
    model: str,
    *,
    limit: int,
    user_id: UUID | None = None,
    run_id: UUID | None = None,
) -> list[tuple[UUID, str]]:
    statement = select(MessageChunk.chunk_id, MessageChunk.content).where(
        needs_embedding(model)
    )
    if run_id is not None:
        statement = statement.join(
            Message, Message.message_id == MessageChunk.message_id
        ).where(Message.run_id == run_id, MessageChunk.user_id == user_id)
    rows = await session.execute(statement.order_by(MessageChunk.chunk_id).limit(limit))
    return [(row.chunk_id, row.content) for row in rows]


async def store_embeddings(
    session: AsyncSession,
    model: str,
    vectors: list[tuple[UUID, list[float]]],
) -> int:
    """只更新仍存在且需要当前模型的块，删除发生在计算期间时无需重建。"""
    updated = 0
    for chunk_id, vector in vectors:
        stored = await session.scalar(
            update(MessageChunk)
            .where(MessageChunk.chunk_id == chunk_id, needs_embedding(model))
            .values(embedding=vector, embedding_model=model)
            .returning(MessageChunk.chunk_id)
        )
        updated += stored is not None
    return updated
