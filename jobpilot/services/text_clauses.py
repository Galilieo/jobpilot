"""用户原话校验共用的中文和英文整句边界。"""

import re
from collections.abc import Iterable

# 逗号不算句子边界，避免把「我接受外包，但仅限暑期岗」或「不要忘掉外包那条」截成半句。
SENTENCE_PUNCTUATION = "。！？；;!?\n"
CLAUSE_PUNCTUATION = SENTENCE_PUNCTUATION
CLAUSE_BOUNDARY = re.compile(f"[{SENTENCE_PUNCTUATION}]")
_SENTENCE_BOUNDARY = frozenset(SENTENCE_PUNCTUATION)


def _touches_boundary(characters: Iterable[str]) -> bool:
    for character in characters:
        if character in _SENTENCE_BOUNDARY:
            return True
        if not character.isspace():
            return False
    return True


def contains_complete_sentence(body: str, quote: str) -> bool:
    """逐字核对一条或相邻几条完整句子，允许省略末尾的句末标点。"""
    quote = quote.strip()
    if not quote or not set(quote) - _SENTENCE_BOUNDARY:
        return False
    for match in re.finditer(re.escape(quote), body):
        if _touches_boundary(reversed(body[: match.start()])) and (
            quote[-1] in _SENTENCE_BOUNDARY or _touches_boundary(body[match.end() :])
        ):
            return True
    return False
