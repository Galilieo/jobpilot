"""技术关键词标准化：模型提取的写法按同义词表映射成标准名，JD 解析和简历匹配共用。

同义词表在 jobpilot/data/tech_keywords.yaml，每项 name、category、aliases。
匹配时忽略大小写、全角半角和首尾空白；表里没有的词原样保留，只去重。
"""

import unicodedata
from functools import cache
from pathlib import Path

import yaml

from jobpilot.config import REPO_ROOT

DEFAULT_TABLE = REPO_ROOT / "jobpilot" / "data" / "tech_keywords.yaml"


class KeywordTableError(ValueError):
    """同义词表格式不对，或同一个别名指向两个标准名。"""


def normalize_key(word: str) -> str:
    """规范化比较键：全半角统一（NFKC）、去首尾空白、折叠大小写。"""
    return unicodedata.normalize("NFKC", word).strip().casefold()


def load_keyword_table(path: Path) -> dict[str, str]:
    """返回「规范化写法 → 标准名」。

    文件不存在时返回空表，同义词表还没整理好时也能用。
    """
    if not path.exists():
        return {}
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    table: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise KeywordTableError(f"{path.name}：每项都要有字符串 name：{entry!r}")
        name = entry["name"].strip()
        for word in [name, *(entry.get("aliases") or [])]:
            key = normalize_key(str(word))
            if table.get(key, name) != name:
                raise KeywordTableError(
                    f"{path.name}：「{word}」同时属于「{table[key]}」和「{name}」"
                )
            table[key] = name
    return table


@cache
def default_table() -> dict[str, str]:
    return load_keyword_table(DEFAULT_TABLE)


def normalize_keywords(
    words: list[str], table: dict[str, str] | None = None
) -> list[str]:
    """映射成标准名并按首次出现的顺序去重；空字符串丢弃。"""
    table = default_table() if table is None else table
    result: list[str] = []
    seen: set[str] = set()
    for word in words:
        key = normalize_key(word)
        if not key:
            continue
        name = table.get(key, unicodedata.normalize("NFKC", word).strip())
        if normalize_key(name) not in seen:
            seen.add(normalize_key(name))
            result.append(name)
    return result
