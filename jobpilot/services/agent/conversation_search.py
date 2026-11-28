"""按需查找本人其他对话的消息，保留原文和跳转所需的出处。"""

from typing import Annotated

from pydantic import Field, StringConstraints

from jobpilot.config import settings
from jobpilot.db.conversation_search import (
    RankedChunk,
    SearchScope,
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
from jobpilot.services.llm_gateway import pii_at_boundary, pii_spans

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


def _snippet(hit: RankedChunk) -> str:
    # 脱敏由模型网关统一做，但切片边界不能把联系方式截成无法识别的两段。
    spans = pii_spans(hit.source_text)
    start = hit.span_start
    end = min(start + _SNIPPET_CHARS, hit.span_end)
    if partial := pii_at_boundary(spans, start):
        start = partial[1]
    if partial := pii_at_boundary(spans, end):
        end = partial[0]
    return hit.source_text[start:end]


def _hit_payload(result: SearchHit) -> Payload:
    hit = result.chunk
    return {
        "conversation_id": str(hit.conversation_id),
        "title": hit.title,
        "seq": hit.seq,
        "role": hit.role,
        "date": hit.date.isoformat(),
        "snippet": _snippet(hit),
        "source": result.source,
    }


async def search_conversations(
    context: ToolContext, args: SearchConversationArgs
) -> Payload:
    agent = context.agent
    scope = SearchScope(agent.user_id, agent.conversation_id)
    model = settings.embedding_model
    query_vector = await _query_vector(args.query, model)
    terms = keyword_query_terms(args.query, DEFAULT_KEYWORD_SELECTION)
    async with read_snapshot(agent.sessions) as session:
        keywords = await keyword_candidates(
            scope,
            session,
            terms,
            args.limit,
            missing_model=model if query_vector is not None else None,
        )
        # 给尚无可比较向量的块保留全文命中的展示位置。
        remaining = args.limit - len(keywords)
        vectors = (
            await vector_candidates(scope, session, model, query_vector, remaining)
            if query_vector is not None and remaining
            else []
        )
    hits = [
        *source_hits(keywords, SearchSource.KEYWORD),
        *source_hits(vectors, SearchSource.VECTOR),
    ]
    return {
        "query": args.query,
        "hits": [_hit_payload(hit) for hit in hits],
        "total": len(hits),
        "degraded": query_vector is None,
    }


async def _query_vector(query: str, model: str) -> list[float] | None:
    try:
        [vector] = await embed_texts([query], model, timeout=_SEARCH_TIMEOUT_SECONDS)
    except EmbeddingUnavailable:
        return None
    return vector
