"""校验 E13 切分、来源标注、重复消息和正式测试封存。"""

from datetime import UTC, datetime
from json import dumps, loads
from pathlib import Path
from shutil import copytree
from uuid import uuid4

import pytest
from evals.runners import e13_dataset
from evals.runners.e13_search import score_query

from jobpilot.db.conversation_search import RankedChunk
from jobpilot.services.conversation_retrieval import SearchHit, SearchSource


@pytest.fixture
def copied_e13(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "e13"
    copytree(e13_dataset.E13_DIR, directory)
    monkeypatch.setattr(e13_dataset, "E13_DIR", directory)
    return directory / "conversations.jsonl"


@pytest.mark.parametrize("split", ["dev", "test"])
def test_e13_queries_support_multiple_sources_and_agent_replies(split: str) -> None:
    samples, queries = e13_dataset.load_e13(split)

    assert len(samples) == 15
    assert len(queries) == 30
    assert any(
        len(query.relevant) > 1 and any(":a" in ref for ref in query.relevant)
        for query in queries
    )


def test_e13_rejects_cross_conversation_duplicates_with_different_whitespace(
    copied_e13: Path,
) -> None:
    rows = [loads(line) for line in copied_e13.read_text().splitlines()]
    original = rows[0]["turns"][0]["user"]
    rows[15]["turns"][0]["assistant"] = "　\t".join(original)
    copied_e13.write_text("\n".join(dumps(row) for row in rows))

    with pytest.raises(ValueError, match="c01 和 c16 存在重复消息"):
        e13_dataset.load_e13("dev")


def test_e13_rejects_edits_to_sealed_test_content(copied_e13: Path) -> None:
    rows = [loads(line) for line in copied_e13.read_text().splitlines()]
    rows[15]["turns"][0]["user"] += " 另有一场复试。"
    copied_e13.write_text("\n".join(dumps(row) for row in rows))

    assert e13_dataset.load_e13("dev")
    with pytest.raises(ValueError, match="正式测试集与封存指纹不一致"):
        e13_dataset.load_e13("test")


def test_e13_rejects_literal_first_paraphrase(copied_e13: Path) -> None:
    path = copied_e13.with_name("queries-dev.jsonl")
    queries = [loads(line) for line in path.read_text().splitlines()]
    next(query for query in queries if query["id"] == "dev-09")["query"] = "PostgreSQL"
    path.write_text("\n".join(dumps(query) for query in queries))

    with pytest.raises(ValueError, match="dev-09.*字面排名第 1"):
        e13_dataset.load_e13("dev")


def test_e13_accepts_paraphrase_with_a_first_ranked_chunk() -> None:
    samples, queries = e13_dataset.load_e13("test")
    query = next(query for query in queries if query.id == "test-09")
    chunks = e13_dataset.corpus_chunks(samples)
    messages = dict(pair for sample in samples for pair in sample.labeled_messages())

    assert e13_dataset.literal_overlap([query], chunks)[query.id].rank == 1
    rank = e13_dataset.message_literal_overlap([query], messages)[query.id].rank
    assert rank is not None and rank > 1


def test_e13_scoring_requires_the_labeled_chunk_of_a_long_reply() -> None:
    source = "甲段解释切换页面。" + "乙段解释断线后读持久化消息。"
    boundary = source.index("乙")
    hits = [
        SearchHit(
            chunk=RankedChunk(
                chunk_id=uuid4(),
                conversation_id=uuid4(),
                title="测试对话",
                seq=3,
                role="assistant",
                date=datetime.now(UTC),
                source_text=source,
                span_start=start,
                span_end=end,
                score=score,
            ),
            source=SearchSource.VECTOR,
            score=score,
        )
        for start, end, score in ((0, boundary, 0.9), (boundary, len(source), 0.8))
    ]
    labels = {3: "c01:a04"}
    query = e13_dataset.Query(
        id="demo", type="gist", query="断线之后怎么继续？", relevant=["c01:a04@9"]
    )
    assert boundary == 9

    wrong = score_query(query, hits[:1], labels, 1)
    correct = score_query(query, hits, labels, 1)
    whole = query.model_copy(update={"relevant": ["c01:a04"]})
    whole_score = score_query(whole, hits, labels, 1)

    assert (wrong.recall_at_5, wrong.reciprocal_rank) == (0, 0)
    assert (correct.recall_at_5, correct.reciprocal_rank) == (1, 0.5)
    assert correct.hits == ["c01:a04@0", "c01:a04@9"]
    assert (whole_score.recall_at_5, whole_score.reciprocal_rank) == (1, 1)
