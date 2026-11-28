"""把结束轮次的原文切块，先提交索引，再异步填充本机向量。"""

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.message_chunks import (
    MIN_MESSAGE_CHARS,
    ChunkSpan,
    IndexChunk,
    chunks_without_search_terms,
    insert_message_chunks,
    message_page_to_index,
    pending_chunks,
    run_messages_to_index,
    store_embeddings,
    store_search_terms,
)
from jobpilot.diagnostics import describe_error
from jobpilot.services.conversation_terms import search_terms
from jobpilot.services.embedding import EmbeddingUnavailable, embed_texts

CHUNK_MAX_CHARS = 500
_PARAGRAPH_BREAK = re.compile(r"\n(?:[^\S\n]*\n)+")
_SENTENCE_END = re.compile(r"[。！？!?；;]+|\.(?=\s|$)")
_MESSAGE_BATCH_SIZE = 64
_EMBEDDING_BATCH_SIZE = 16
_EMBEDDING_TIMEOUT = 60

logger = logging.getLogger("uvicorn.error").getChild("jobpilot")


def _paragraph_chunks(text: str, start: int, end: int) -> list[ChunkSpan]:
    chunks = []
    sentence_ends = [match.end() for match in _SENTENCE_END.finditer(text, start, end)]
    while end - start > CHUNK_MAX_CHARS:
        cut = start + CHUNK_MAX_CHARS
        boundary = bisect_right(sentence_ends, cut)
        if boundary and sentence_ends[boundary - 1] > start:
            cut = sentence_ends[boundary - 1]
        chunks.append(ChunkSpan(start, cut))
        start = cut
    chunks.append(ChunkSpan(start, end))
    return chunks


def chunk_spans(content: str) -> list[ChunkSpan]:
    """保留 Python 字符偏移和原始分隔符，便于按 span 找回逐字原文。"""
    if len(content.strip()) < MIN_MESSAGE_CHARS:
        return []
    if len(content) <= CHUNK_MAX_CHARS:
        return [ChunkSpan(0, len(content))]
    ends = [match.end() for match in _PARAGRAPH_BREAK.finditer(content)]
    if not ends or ends[-1] != len(content):
        ends.append(len(content))
    return [
        span
        for start, end in zip([0, *ends[:-1]], ends, strict=True)
        for span in _paragraph_chunks(content, start, end)
    ]


def _index_chunks(content: str) -> list[IndexChunk]:
    chunks = []
    for span in chunk_spans(content):
        text = content[span.start : span.end]
        if text.strip():
            chunks.append(IndexChunk(span, text, search_terms(text)))
    return chunks


@dataclass(frozen=True)
class IndexResult:
    chunks_created: int
    embeddings_written: int
    embedding_unavailable: bool


@dataclass(frozen=True)
class _IndexRun:
    user_id: UUID
    run_id: UUID


async def _fill_embeddings(
    sessions: async_sessionmaker[AsyncSession], run: _IndexRun | None = None
) -> tuple[int, bool]:
    model = settings.embedding_model
    updated = 0
    while True:
        async with sessions() as session:
            pending = await pending_chunks(
                session,
                model,
                limit=_EMBEDDING_BATCH_SIZE,
                user_id=run.user_id if run else None,
                run_id=run.run_id if run else None,
            )
        if not pending:
            return updated, False
        try:
            vectors = await embed_texts(
                [text for _, text in pending], model, timeout=_EMBEDDING_TIMEOUT
            )
        except EmbeddingUnavailable:
            return updated, True
        paired = [
            (chunk_id, vector)
            for (chunk_id, _), vector in zip(pending, vectors, strict=True)
        ]
        async with sessions.begin() as session:
            updated += await store_embeddings(session, model, paired)


async def index_finished_run(
    sessions: async_sessionmaker[AsyncSession], user_id: UUID, run_id: UUID
) -> None:
    """索引失败可由离线脚本补算，已经提交的聊天状态不受影响。"""
    try:
        async with sessions.begin() as session:
            messages = await run_messages_to_index(user_id, session, run_id)
            for message in messages:
                await insert_message_chunks(
                    session, message, _index_chunks(message.content)
                )
        if messages:
            await _fill_embeddings(sessions, _IndexRun(user_id, run_id))
    except SQLAlchemyError as error:
        logger.warning("对话分块保存未完成（%s）", describe_error(error))


async def _backfill_chunks(sessions: async_sessionmaker[AsyncSession]) -> int:
    created = 0
    after_seq = 0
    while True:
        async with sessions.begin() as session:
            messages = await message_page_to_index(
                session, after_seq=after_seq, limit=_MESSAGE_BATCH_SIZE
            )
            if not messages:
                return created
            for message in messages:
                created += await insert_message_chunks(
                    session, message, _index_chunks(message.content)
                )
            after_seq = messages[-1].seq


async def _backfill_search_terms(sessions: async_sessionmaker[AsyncSession]) -> None:
    while True:
        async with sessions.begin() as session:
            missing = await chunks_without_search_terms(
                session, limit=_MESSAGE_BATCH_SIZE
            )
            if not missing:
                return
            for chunk_id, text in missing:
                await store_search_terms(session, chunk_id, search_terms(text))


async def backfill_message_index(
    sessions: async_sessionmaker[AsyncSession],
) -> IndexResult:
    """先补齐全部原文块，Ollama 停机时仍可按关键字搜索。"""
    created = await _backfill_chunks(sessions)
    await _backfill_search_terms(sessions)
    embedded, unavailable = await _fill_embeddings(sessions)
    return IndexResult(created, embedded, unavailable)
