"""校验 E13 切分、来源标注、重复消息和正式测试封存。"""

from json import dumps, loads
from pathlib import Path
from shutil import copytree

import pytest
from evals.runners import e13_dataset


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
