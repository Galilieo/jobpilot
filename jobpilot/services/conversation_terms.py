"""分词生成索引文本，并为全文查询选择需要匹配的词。"""

from collections.abc import Iterable
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING

import jieba

if TYPE_CHECKING:
    from jieba.analyse import TFIDF

_TOKENIZER = jieba.Tokenizer()
TFIDF_KEYWORD_LIMIT = 5


class KeywordSelection(StrEnum):
    ALL = "all"
    TFIDF = "tfidf"


DEFAULT_KEYWORD_SELECTION = KeywordSelection.TFIDF


def search_terms(text: str) -> str:
    """保留现有索引分词，兼容已存入 PostgreSQL 的词及位置。"""
    return " ".join(token for token in _TOKENIZER.cut(text) if token.strip())


@cache
def _keyword_extractor() -> "TFIDF":
    from jieba.analyse import TFIDF

    extractor = TFIDF()
    extractor.tokenizer = _TOKENIZER
    extractor.stop_words.clear()
    return extractor


def _searchable_words(tokens: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(token for token in tokens if any(map(str.isalnum, token)))
    )


def keyword_query_terms(text: str, selection: KeywordSelection) -> list[str]:
    """过滤标点，保留技术名中的符号。关键词为空时回退到可用分词。"""
    if selection == KeywordSelection.TFIDF:
        keywords = _searchable_words(_keyword_extractor().extract_tags(text, topK=None))
        if keywords:
            return keywords[:TFIDF_KEYWORD_LIMIT]
    return _searchable_words(_TOKENIZER.cut(text))
