"""识别记忆原话中的栏目和操作，避免把正文里的任意词当成授权。"""

import re
from enum import StrEnum
from functools import cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jobpilot.config import REPO_ROOT
from jobpilot.db.memories import MemoryValue
from jobpilot.db.memory_schema import MEMORY_LIMITS, MemoryKey

_CLAUSE_BOUNDARY = re.compile(r"[。！？；，,;!?\n]")


class MemoryOperation(StrEnum):
    SET = "set"
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    CLEAR = "clear"


class MemoryWords(BaseModel):
    label: str
    group: Literal["job", "writing"]
    aliases: list[str]
    examples: list[str] = Field(default_factory=list)
    values: dict[str, list[str]] = Field(default_factory=dict)


class Phrases(BaseModel):
    lasting: list[str]
    negative: list[str]
    add: list[str]
    remove: list[str]
    replace: list[str]
    clear: list[str]
    keep: list[str]


class Vocabulary(BaseModel):
    keys: dict[MemoryKey, MemoryWords]
    phrases: Phrases


@cache
def memory_vocabulary() -> Vocabulary:
    path = REPO_ROOT / "jobpilot" / "data" / "memory_keys.yaml"
    vocabulary = Vocabulary.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if vocabulary.keys.keys() != MEMORY_LIMITS.keys() or any(
        words.values.keys() != set(MEMORY_LIMITS[key].choices)
        for key, words in vocabulary.keys.items()
    ):
        raise ValueError("记忆词表与栏目定义不一致")
    return vocabulary


def _mentions(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def _clause(body: str, quote: str) -> str | None:
    if body.count(quote) != 1:
        return None
    before, _, after = body.partition(quote)
    return _CLAUSE_BOUNDARY.split(before)[-1] + quote + _CLAUSE_BOUNDARY.split(after)[0]


def _value_evidence(
    words: MemoryWords, value: MemoryValue | None, quote: str
) -> list[str]:
    if isinstance(value, list):
        return value if value and all(item in quote for item in value) else []
    if value is None:
        return []
    options = [value, *words.values.get(value, [])]
    return [option for option in options if option in quote]


def _operation_supported(
    op: MemoryOperation, clause: str, evidence: list[str], phrases: Phrases
) -> bool:
    # 枚举的规范值和同义说法本身可能是否定表达，例如「别太花哨」。
    surrounding = clause
    for phrase in sorted(evidence, key=len, reverse=True):
        surrounding = surrounding.replace(phrase, "")
    if op in {MemoryOperation.SET, MemoryOperation.ADD, MemoryOperation.REPLACE}:
        if _mentions(surrounding, phrases.negative):
            return False
    if op == MemoryOperation.SET:
        return _mentions(surrounding, [*phrases.lasting, *phrases.replace])
    if op == MemoryOperation.ADD and _mentions(surrounding, phrases.replace):
        return False
    return _mentions(surrounding, getattr(phrases, op.value))


def supports_memory_change(
    key: MemoryKey,
    operation: MemoryOperation,
    value: MemoryValue | None,
    quote: str,
    body: str,
    attachments: list[str],
) -> bool:
    """将一个修改绑定到本轮正文中的单一、有辨识度的原话。"""
    clause = _clause(body, quote)
    if clause is None or any(quote in attachment for attachment in attachments):
        return False
    vocabulary = memory_vocabulary()
    words = vocabulary.keys[key]
    mentions_key = _mentions(clause, [words.label, *words.aliases])
    if operation in {MemoryOperation.CLEAR, MemoryOperation.REMOVE} and _mentions(
        clause, vocabulary.phrases.keep
    ):
        return False
    if operation == MemoryOperation.CLEAR:
        return _mentions(quote, [words.label, *words.aliases]) and _mentions(
            clause, vocabulary.phrases.clear
        )
    evidence = _value_evidence(words, value, quote)
    if not evidence:
        return False
    implicit_key = bool(words.values) or any(
        item in words.examples for item in evidence
    )
    if not (mentions_key or implicit_key):
        return False
    if any(all(item in attachment for item in evidence) for attachment in attachments):
        return False
    return _operation_supported(operation, clause, evidence, vocabulary.phrases)
