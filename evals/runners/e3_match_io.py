"""评测逐步记录的原子 JSON 写入。"""

import json
from pathlib import Path
from typing import Any


def write_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
