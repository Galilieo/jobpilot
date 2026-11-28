"""共享跨对话检索的来源标记和按排名融合，供工具与 E13 使用。"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from jobpilot.db.conversation_search import RankedChunk

FUSION_CANDIDATE_LIMIT = 20
_RRF_RANK_CONSTANT = 60


class SearchSource(StrEnum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    BOTH = "both"


@dataclass(frozen=True)
class SearchHit:
    chunk: RankedChunk
    source: SearchSource
    score: float


def source_hits(rows: list[RankedChunk], source: SearchSource) -> list[SearchHit]:
    return [SearchHit(row, source, row.score) for row in rows]


def fuse_rankings(
    vectors: list[RankedChunk], keywords: list[RankedChunk], limit: int
) -> list[SearchHit]:
    """相加每条结果的倒数名次，避免直接比较两种处于不同尺度的分数。"""
    scores: dict[UUID, SearchHit] = {}
    for ranking, source in (
        (vectors, SearchSource.VECTOR),
        (keywords, SearchSource.KEYWORD),
    ):
        for rank, chunk in enumerate(ranking, 1):
            score = 1 / (_RRF_RANK_CONSTANT + rank)
            previous = scores.get(chunk.chunk_id)
            scores[chunk.chunk_id] = (
                SearchHit(chunk, SearchSource.BOTH, score + previous.score)
                if previous
                else SearchHit(chunk, source, score)
            )
    return sorted(scores.values(), key=lambda hit: -hit.score)[:limit]
