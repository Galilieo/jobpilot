"""按需查找本人其他对话的消息，保留原文和跳转所需的出处。"""

from typing import Annotated

from pydantic import Field, StringConstraints

from jobpilot.config import settings
from jobpilot.db.conversation_search import (
    RankedChunk,
    SearchScope,
    has_pending_embeddings,
    keyword_candidates,
    vector_candidates,
)
from jobpilot.db.session import read_snapshot
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext
from jobpilot.services.conversation_retrieval import (
    SearchHit,
    SearchSource,
    source_hits,
)
from jobpilot.services.conversation_terms import (
    DEFAULT_KEYWORD_SELECTION,
    keyword_query_terms,
)
from jobpilot.services.embedding import EmbeddingUnavailable, embed_texts
from jobpilot.services.llm_gateway import pii_at_boundary, pii_spans, redact_pii

_SNIPPET_CHARS = 300
_SEARCH_TIMEOUT_SECONDS = 5


class SearchConversationArgs(ToolArgs):
    query: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\x00]+$"
        ),
    ]
    limit: int = Field(default=5, ge=1, le=8)


def snippet_for_span(
    source_text: str, span_start: int | None, span_end: int | None
) -> str:
    """按字符范围截取摘要，避开被截断的联系方式后再脱敏。"""
    if not source_text:
        return ""
    length = len(source_text)
    raw_start = 0 if span_start is None else span_start
    raw_end = length if span_end is None else span_end
    start = max(0, min(raw_start, length))
    end = max(start, min(start + _SNIPPET_CHARS, raw_end, length))
    spans = pii_spans(source_text)
    if partial := pii_at_boundary(spans, start):
        start = partial[1]
    if partial := pii_at_boundary(spans, end):
        end = partial[0]
    if end <= start:
        return ""
    return redact_pii(source_text[start:end])


def _hit_payload(result: SearchHit) -> Payload:
    hit = result.chunk
    return {
        "conversation_id": str(hit.conversation_id),
        "title": hit.title,
        "seq": hit.seq,
        "role": hit.role,
        "date": hit.date.isoformat(),
        "snippet": snippet_for_span(hit.source_text, hit.span_start, hit.span_end),
        "source": result.source,
        "_span_start": hit.span_start,
        "_span_end": hit.span_end,
    }


def _merge_hits(
    vectors: list[RankedChunk], pending: list[RankedChunk], limit: int
) -> list[SearchHit]:
    hits = source_hits(vectors, SearchSource.VECTOR)
    seen = {hit.chunk.content for hit in hits}
    for chunk in pending:
        if len(hits) >= limit:
            break
        if chunk.content in seen:
            continue
        seen.add(chunk.content)
        hits.append(SearchHit(chunk, SearchSource.KEYWORD, chunk.score))
    return hits[:limit]


async def search_conversations(
    context: ToolContext, args: SearchConversationArgs
) -> Payload:
    agent = context.agent
    scope = SearchScope(agent.user_id, agent.conversation_id)
    model = settings.embedding_model
    query_vector = await _query_vector(args.query, model)
    terms = keyword_query_terms(args.query, DEFAULT_KEYWORD_SELECTION)
    async with read_snapshot(agent.sessions) as session:
        index_pending = await has_pending_embeddings(scope, session, model)
        if query_vector is None:
            keywords = await keyword_candidates(scope, session, terms, args.limit)
            hits = source_hits(keywords, SearchSource.KEYWORD)
        else:
            vectors = await vector_candidates(
                scope, session, model, query_vector, args.limit
            )
            pending = (
                await keyword_candidates(
                    scope, session, terms, args.limit, missing_model=model
                )
                if index_pending and len(vectors) < args.limit
                else []
            )
            hits = _merge_hits(vectors, pending, args.limit)
    return {
        "query": args.query,
        "hits": [_hit_payload(hit) for hit in hits],
        "total": len(hits),
        "degraded": query_vector is None,
        "index_pending": index_pending,
    }


async def _query_vector(query: str, model: str) -> list[float] | None:
    try:
        [vector] = await embed_texts([query], model, timeout=_SEARCH_TIMEOUT_SECONDS)
    except EmbeddingUnavailable:
        return None
    return vector
