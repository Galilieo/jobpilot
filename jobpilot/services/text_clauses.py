"""用户原话校验共用的中文和英文短句边界。"""

import re

CLAUSE_PUNCTUATION = "。！？；，,;!?\n"
CLAUSE_BOUNDARY = re.compile(f"[{CLAUSE_PUNCTUATION}]")
