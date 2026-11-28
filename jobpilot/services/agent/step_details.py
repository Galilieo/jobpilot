"""工具步骤给界面展开看的细节：读了哪些岗位、事实库第几版、多少条。

只存在消息的步骤记录里给前端显示，回传模型前用 ``model_view`` 去掉，模型输入不变。
不负责步骤摘要本身（见 ``context.summarize_tool``）。
"""

from collections.abc import Callable
from typing import Any

from jobpilot.services.agent.types import Payload

DETAIL_KEY = "detail"
# 岗位名、公司来自 JD，界面上一行放得下即可。
_TEXT_LIMIT = 60
# 匹配报告的主要差距只列前几条，展开后一眼能看完。
_MAX_GAPS = 3


def _short(value: Any) -> str | None:
    return value[:_TEXT_LIMIT] if isinstance(value, str) and value else None


def _job_fields(fields: Payload) -> Payload:
    return {
        "title": _short(fields.get("title")),
        "company": _short(fields.get("company")),
        "location": _short(fields.get("location")),
    }


def _list_jobs(result: Payload) -> Payload:
    return {
        "jobs": [
            {"job_id": job["job_id"], **_job_fields(job.get("fields") or {})}
            for job in result.get("jobs", [])
        ]
    }


def _read_job(result: Payload) -> Payload:
    return _job_fields((result.get("parsed") or {}).get("basic_fields") or {})


def _read_facts(result: Payload) -> Payload:
    return {
        "version": result.get("version"),
        "facts": len(result.get("facts", [])),
        "criteria": len(result.get("criteria", [])),
        "education": len(result.get("education", [])),
    }


def _read_match_report(result: Payload) -> Payload:
    gaps = [gap for gap in result.get("main_gaps", []) if isinstance(gap, str)]
    return {
        "items": len(result.get("items", [])),
        "main_gaps": [gap[:_TEXT_LIMIT] for gap in gaps[:_MAX_GAPS]],
    }


def _read_resume(result: Payload) -> Payload:
    return {"version": result.get("version")}


def _memory_record(result: Payload) -> Payload:
    return {
        key: result[key]
        for key in ("content", "field", "value", "quote", "action_id")
        if key in result
    }


_DETAILS: dict[str, Callable[[Payload], Payload]] = {
    "list_jobs": _list_jobs,
    "read_job": _read_job,
    "read_facts": _read_facts,
    "read_match_report": _read_match_report,
    "read_resume": _read_resume,
    "remember": _memory_record,
    "forget": _memory_record,
    "set_writing_preference": _memory_record,
}


def step_detail(name: str, result: Payload) -> Payload | None:
    """出错的步骤没有细节，界面只显示原因。"""
    build = _DETAILS.get(name)
    return build(result) if build and "error" not in result else None


def model_view(step: Payload) -> Payload:
    return {key: value for key, value in step.items() if key != DETAIL_KEY}
