"""为应用日志提取可公开的错误类型、错误码和调用位置。"""

import traceback
from pathlib import Path

from pydantic import ValidationError

from jobpilot.config import REPO_ROOT


def describe_error(error: BaseException) -> str:
    """仅提取类型和错误码，异常说明可能包含整行数据或输入值。"""
    parts = [type(error).__name__]
    original = getattr(error, "orig", None)  # SQLAlchemy 包在里面的数据库异常
    if original is not None:
        parts.append(type(original).__name__)
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        if constraint:
            parts.append(constraint)
        if getattr(original, "sqlstate", None):
            parts.append(original.sqlstate)
    if isinstance(error, ValidationError):
        parts.append(
            ",".join(
                sorted(
                    {
                        item["type"]
                        for item in error.errors(
                            include_input=False,
                            include_context=False,
                            include_url=False,
                        )
                    }
                )
            )
        )
    return " · ".join(parts)


def error_locations(error: BaseException) -> str:
    """从 traceback 提取文件、行号与函数名。"""
    frames = []
    for frame, lineno in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        try:
            display = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            display = path.name
        frames.append(f"{display}:{lineno} {frame.f_code.co_name}")
    return " -> ".join(frames) or "unknown"
