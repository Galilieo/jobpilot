"""关键词标准化：别名映射、大小写与全角、去重、同义词表的校验。"""

from pathlib import Path

import pytest

from jobpilot.services.keywords import (
    DEFAULT_TABLE,
    KeywordTableError,
    load_keyword_table,
    normalize_keywords,
)

TABLE_YAML = """
- name: LLM
  category: ai
  aliases: [大模型, 大语言模型, Large Language Model]
- name: Kubernetes
  category: infra
  aliases: [k8s]
- name: Go
  category: language
  aliases: [Golang]
"""


@pytest.fixture
def table(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "tech_keywords.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    return load_keyword_table(path)


def test_aliases_map_to_standard_names(table: dict[str, str]) -> None:
    assert normalize_keywords(["大模型", "K8S", "golang", "Python"], table) == [
        "LLM",
        "Kubernetes",
        "Go",
        "Python",  # 表里没有的词原样保留
    ]


def test_case_width_and_duplicates(table: dict[str, str]) -> None:
    # 全角字母先转半角；同一个标准名只保留第一次出现
    assert normalize_keywords(["ＬＬＭ", " llm ", "大语言模型", "", "  "], table) == [
        "LLM"
    ]


def test_unknown_words_are_deduplicated_ignoring_case(table: dict[str, str]) -> None:
    assert normalize_keywords(["FastAPI", "fastapi"], table) == ["FastAPI"]


def test_missing_table_keeps_words(tmp_path: Path) -> None:
    assert load_keyword_table(tmp_path / "absent.yaml") == {}


def test_conflicting_alias_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "- name: Go\n  aliases: [GO语言]\n- name: Golang\n  aliases: [go]\n",
        encoding="utf-8",
    )

    with pytest.raises(KeywordTableError, match="同时属于"):
        load_keyword_table(path)


@pytest.mark.skipif(not DEFAULT_TABLE.exists(), reason="同义词表还没整理好")
def test_project_table_is_valid() -> None:
    """仓库里的同义词表能解析、标准名和别名没有冲突。"""
    assert load_keyword_table(DEFAULT_TABLE)
