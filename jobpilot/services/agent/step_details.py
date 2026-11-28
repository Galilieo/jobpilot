"""整理工具步骤展开时显示的业务摘要和会话搜索出处。

只存在消息的步骤记录里给前端显示，回传模型前用 ``model_view`` 去掉，模型输入不变。
不负责步骤摘要本身（见 ``context.summarize_tool``）。
"""

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversation_search import load_message_sources
from jobpilot.db.models import Message
from jobpilot.services.agent.conversation_search import snippet_for_span
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


def _stored_search_hit(hit: Payload) -> Payload | None:
    conversation_id = hit.get("conversation_id")
    seq = hit.get("seq")
    if not conversation_id or not isinstance(seq, int):
        return None
    stored: Payload = {"conversation_id": str(conversation_id), "seq": seq}
    for out_key, in_keys in (
        ("span_start", ("_span_start", "span_start")),
        ("span_end", ("_span_end", "span_end")),
    ):
        for key in in_keys:
            if isinstance(hit.get(key), int):
                stored[out_key] = hit[key]
                break
    if isinstance(hit.get("source"), str):
        stored["source"] = hit["source"]
    return stored


def _conversation_search(result: Payload) -> Payload:
    hits = [
        stored
        for hit in result.get("hits", [])
        if isinstance(hit, dict) and (stored := _stored_search_hit(hit)) is not None
    ]
    return {
        "query": result["query"],
        "hits": hits,
        "degraded": result["degraded"],
        "index_pending": bool(result.get("index_pending")),
    }


def _live_conversation_search(result: Payload) -> Payload:
    hits = [
        {k: v for k, v in hit.items() if not (isinstance(k, str) and k.startswith("_"))}
        for hit in result.get("hits", [])
        if isinstance(hit, dict)
    ]
    return {
        "query": result["query"],
        "hits": hits,
        "degraded": result["degraded"],
        "index_pending": bool(result.get("index_pending")),
    }


def strip_search_step_excerpts(steps: list[Payload]) -> list[Payload]:
    """清理旧消息步骤里的外部对话标题与摘录副本，仅保留定位信息。"""
    cleaned: list[Payload] = []
    for step in steps:
        if (
            isinstance(step, dict)
            and step.get("tool") == "search_conversations"
            and isinstance(step.get("detail"), dict)
        ):
            detail = dict(step["detail"])
            detail["hits"] = [
                stored
                for hit in detail.get("hits", [])
                if isinstance(hit, dict)
                and (stored := _stored_search_hit(hit)) is not None
            ]
            cleaned.append({**step, "detail": detail})
        else:
            cleaned.append(step)
    return cleaned


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
    "search_conversations": _conversation_search,
    "remember": _memory_record,
    "forget": _memory_record,
    "set_writing_preference": _memory_record,
}


def step_detail(name: str, result: Payload) -> Payload | None:
    """出错的步骤没有细节，界面只显示原因。"""
    build = _DETAILS.get(name)
    return build(result) if build and "error" not in result else None


def live_step_detail(name: str, result: Payload) -> Payload | None:
    """运行中实时事件可带当次标题和摘要，落盘步骤只保留定位。"""
    if name == "search_conversations" and "error" not in result:
        return _live_conversation_search(result)
    return step_detail(name, result)


def model_view(step: Payload) -> Payload:
    return {key: value for key, value in step.items() if key != DETAIL_KEY}


def _locator_of(hit: Payload) -> tuple[UUID, int] | None:
    seq = hit.get("seq")
    if not isinstance(seq, int):
        return None
    try:
        return UUID(str(hit.get("conversation_id"))), seq
    except ValueError:
        return None


def _resolved_hit(
    hit: Payload, sources: dict[tuple[UUID, int], tuple[str, str]]
) -> Payload:
    base: Payload = {
        "conversation_id": hit["conversation_id"],
        "seq": hit["seq"],
        **({"source": hit["source"]} if "source" in hit else {}),
    }
    locator = _locator_of(hit)
    source = sources.get(locator) if locator is not None else None
    if source is None:
        return {**base, "source_deleted": True}
    title, content = source
    snippet = snippet_for_span(content, hit.get("span_start"), hit.get("span_end"))
    return {**base, "title": title, "snippet": snippet, "source_deleted": False}


async def resolve_search_steps(
    session: AsyncSession,
    user_id: UUID,
    messages: Sequence[Message],
) -> dict[UUID, list[Payload]]:
    """读取或重放消息时按当前权限实时回填跨对话搜索的标题与摘要。"""
    stripped: dict[UUID, list[Payload]] = {}
    locators: set[tuple[UUID, int]] = set()
    for message in messages:
        if not any(
            isinstance(step, dict) and step.get("tool") == "search_conversations"
            for step in message.steps_json
        ):
            continue
        cleaned = strip_search_step_excerpts(message.steps_json)
        stripped[message.message_id] = cleaned
        for step in cleaned:
            if step.get("tool") == "search_conversations" and isinstance(
                detail := step.get("detail"), dict
            ):
                for hit in detail.get("hits", []):
                    if (locator := _locator_of(hit)) is not None:
                        locators.add(locator)
    if not stripped:
        return {}
    sources = await load_message_sources(session, user_id, list(locators))
    resolved: dict[UUID, list[Payload]] = {}
    for message_id, steps in stripped.items():
        resolved[message_id] = [
            (
                {
                    **step,
                    "detail": {
                        **step["detail"],
                        "hits": [
                            _resolved_hit(hit, sources)
                            for hit in step["detail"].get("hits", [])
                        ],
                    },
                }
                if step.get("tool") == "search_conversations"
                and isinstance(step.get("detail"), dict)
                else step
            )
            for step in steps
        ]
    return resolved
