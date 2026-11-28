"""在独立 PostgreSQL 账号上对照向量、关键词和 RRF，不调用聊天模型。"""

import argparse
import asyncio
import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from json import dumps, loads
from math import ceil
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.runners.e13_dataset import (
    DATASET_VERSION,
    ConversationSample,
    NoAnswerKind,
    Query,
    Split,
    chunk_reference,
    dataset_digest,
    load_e13,
    matching_refs,
    seed_conversations,
)
from jobpilot.config import REPO_ROOT, settings
from jobpilot.db.conversation_search import (
    SearchScope,
    keyword_candidates,
    vector_candidates,
)
from jobpilot.db.models import Conversation, User
from jobpilot.db.session import create_database_engine
from jobpilot.services.conversation_retrieval import (
    FUSION_CANDIDATE_LIMIT,
    SearchHit,
    SearchSource,
    fuse_rankings,
    source_hits,
)
from jobpilot.services.conversation_terms import search_terms
from jobpilot.services.embedding import embed_texts
from jobpilot.services.message_index import backfill_message_index

RESULTS_DIR = REPO_ROOT / "evals" / "results"
TOP_K = 5
QUERY_TIMEOUT_SECONDS = 5
FUSION_MIN_RECALL_GAIN = 0.125
FUSION_MAX_MRR_LOSS = 0.03
FUSION_MAX_ADDED_P95_MS = 100


class Method(StrEnum):
    VECTOR = "A_vector"
    KEYWORD = "B_keyword"
    FUSION = "C_rrf"


@dataclass(frozen=True)
class QueryResult:
    query_id: str
    query_type: str
    no_answer_kind: NoAnswerKind | None
    recall_at_5: float | None
    reciprocal_rank: float | None
    top_score: float | None
    elapsed_ms: float
    hits: list[str]


def _ms_since(started: float) -> float:
    return 1000 * (perf_counter() - started)


async def _run_query(
    session: AsyncSession, scope: SearchScope, query: Query
) -> dict[Method, tuple[list[SearchHit], float]]:
    started = perf_counter()
    [embedding] = await embed_texts(
        [query.query], settings.embedding_model, timeout=QUERY_TIMEOUT_SECONDS
    )
    vectors = await vector_candidates(
        scope, session, settings.embedding_model, embedding, FUSION_CANDIDATE_LIMIT
    )
    vector_ms = _ms_since(started)

    started = perf_counter()
    keywords = await keyword_candidates(
        scope, session, search_terms(query.query), FUSION_CANDIDATE_LIMIT
    )
    keyword_ms = _ms_since(started)

    started = perf_counter()
    fused = fuse_rankings(vectors, keywords, FUSION_CANDIDATE_LIMIT)
    fusion_ms = _ms_since(started)
    return {
        Method.VECTOR: (source_hits(vectors, SearchSource.VECTOR), vector_ms),
        Method.KEYWORD: (source_hits(keywords, SearchSource.KEYWORD), keyword_ms),
        Method.FUSION: (fused, vector_ms + keyword_ms + fusion_ms),
    }


def score_query(
    query: Query,
    hits: list[SearchHit],
    message_labels: dict[int, str],
    elapsed_ms: float,
) -> QueryResult:
    """命中要满足标注粒度。重复块占展示名次，每个标注只计一次召回。"""
    retrieved = [
        chunk_reference(message_labels[hit.chunk.seq], hit.chunk.span_start)
        for hit in hits
    ]
    relevant = set(query.relevant)
    matches = [matching_refs(label, relevant) for label in retrieved]
    recalled = set().union(*matches[:TOP_K])
    recall = len(recalled) / len(relevant) if relevant else None
    rank = next((rank for rank, sources in enumerate(matches, 1) if sources), None)
    return QueryResult(
        query_id=query.id,
        query_type=query.type,
        no_answer_kind=query.no_answer_kind,
        recall_at_5=recall,
        reciprocal_rank=(1 / rank if rank else 0.0) if relevant else None,
        top_score=hits[0].score if hits else None,
        elapsed_ms=round(elapsed_ms, 3),
        hits=retrieved[:TOP_K],
    )


def _summarize(rows: list[QueryResult]) -> dict:
    answered = [row for row in rows if row.recall_at_5 is not None]
    elapsed = sorted(row.elapsed_ms for row in rows)
    return {
        "recall_at_5": mean(row.recall_at_5 for row in answered),
        "mrr_at_20": mean(row.reciprocal_rank for row in answered),
        "p50_ms": median(elapsed),
        "p95_ms": elapsed[ceil(len(elapsed) * 0.95) - 1],
        "no_answer_top_scores": [
            {
                "query_id": row.query_id,
                "kind": row.no_answer_kind,
                "score": row.top_score,
            }
            for row in rows
            if row.query_type == "no_answer"
        ],
    }


def dev_recommendation(summary: dict[Method, dict]) -> Method:
    baseline, fusion = summary[Method.VECTOR], summary[Method.FUSION]
    recall_gain = round(fusion["recall_at_5"] - baseline["recall_at_5"], 6)
    keep_fusion = (
        recall_gain >= FUSION_MIN_RECALL_GAIN
        and fusion["mrr_at_20"] >= baseline["mrr_at_20"] - FUSION_MAX_MRR_LOSS
        and fusion["p95_ms"] <= baseline["p95_ms"] + FUSION_MAX_ADDED_P95_MS
    )
    return Method.FUSION if keep_fusion else Method.VECTOR


async def _evaluate(
    sessions: async_sessionmaker[AsyncSession],
    scope: SearchScope,
    queries: list[Query],
    labels: dict[int, str],
) -> dict[Method, list[QueryResult]]:
    rows: dict[Method, list[QueryResult]] = {method: [] for method in Method}
    for query in queries:
        async with sessions() as session:
            rankings = await _run_query(session, scope, query)
        for method, (hits, elapsed_ms) in rankings.items():
            rows[method].append(score_query(query, hits, labels, elapsed_ms))
    return rows


async def run(
    samples: list[ConversationSample], queries: list[Query]
) -> dict[Method, list[QueryResult]]:
    url = settings.database_url
    if (
        url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.database != "jobpilot_e13"
    ):
        raise ValueError("E13 只允许连接本机独立数据库 jobpilot_e13")
    engine = create_database_engine(
        url, connect_timeout_seconds=settings.database_connect_timeout_seconds
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid4()
    try:
        async with sessions.begin() as session:
            session.add(
                User(
                    user_id=user_id,
                    email=f"e13-{user_id}@example.invalid",
                    hashed_password="synthetic-fixture",
                )
            )
            await session.flush()
            labels = await seed_conversations(session, user_id, samples)
            target = Conversation(user_id=user_id, title="E13 发起搜索")
            session.add(target)
        index = await backfill_message_index(sessions)
        if index.embedding_unavailable:
            raise RuntimeError("E13 需要完成所有消息向量后再搜索")
        scope = SearchScope(user_id, target.conversation_id)
        return await _evaluate(sessions, scope, queries, labels)
    finally:
        try:
            async with sessions.begin() as session:
                await session.execute(delete(User).where(User.user_id == user_id))
        finally:
            await engine.dispose()


def _result_path(split: Split) -> Path:
    return RESULTS_DIR / f"e13-{split}-{dataset_digest(split)[:12]}.json"


def _dev_choice(split: Split) -> Method | None:
    if split == "dev":
        return None
    dev_path = _result_path("dev")
    if not dev_path.exists():
        raise SystemExit("正式测试前需要先完成开发集并冻结取舍")
    result = loads(dev_path.read_text(encoding="utf-8"))
    if result["embedding_model"] != settings.embedding_model:
        raise SystemExit("正式测试必须沿用开发集的 Embedding 模型")
    return Method(result["selected_on_dev"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    split: Split = args.split
    samples, queries = load_e13(split)
    digest = dataset_digest(split)
    if args.validate_only:
        print(
            dumps(
                {
                    "version": DATASET_VERSION,
                    "conversations": len(samples),
                    "queries": len(queries),
                    "sha256": digest,
                }
            )
        )
        return
    path = _result_path(split)
    if path.exists():
        raise SystemExit(f"此数据集已有 E13 {split} 结果：{path}")
    chosen = _dev_choice(split)
    rows = asyncio.run(run(samples, queries))
    summary = {method: _summarize(results) for method, results in rows.items()}
    payload = {
        "split": split,
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": digest,
        "embedding_model": settings.embedding_model,
        "selected_on_dev": chosen or dev_recommendation(summary),
        "summary": summary,
        "queries": {
            method: [asdict(row) for row in results] for method, results in rows.items()
        },
    }
    with path.open("x", encoding="utf-8") as file:
        file.write(dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        dumps({"report": str(path), "summary": summary}, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
