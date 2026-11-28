"""为消息正文和查询使用同一套中文分词规则。"""

import jieba

_TOKENIZER = jieba.Tokenizer()


def search_terms(text: str) -> str:
    return " ".join(token for token in _TOKENIZER.cut(text) if token.strip())
