"""JD 解析：模型整理出基本字段、要求、职责和关键词，程序校验片段、标准化关键词。

和事实拆分一样，模型只给片段文字，位置由程序在原文中查找；找不到的条目标为未通过校验，
提醒用户核对，但不丢弃（JD 不会被发布成事实，错一条的代价是匹配时多看一眼）。
关键词按同义词表映射成标准名；注入检测在 services/injection.py，与模型无关。
"""

from enum import StrEnum
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.services.facts import TruncatedOutputError, locate_quote
from jobpilot.services.keywords import normalize_keywords
from jobpilot.services.llm_gateway import (
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt

# ---------- 模型输出：字段不全或多出字段都校验失败，触发网关的 JSON 修复 ----------


class _ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BasicFields(_ModelOutput):
    """JD 没写的为 None，不猜测；前后矛盾的也为 None，由用户核对。"""

    company: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)
    salary: str | None = Field(default=None, max_length=100)
    onsite_days_per_week: str | None = Field(default=None, max_length=100)
    internship_duration: str | None = Field(default=None, max_length=100)
    education: str | None = Field(default=None, max_length=100)
    graduation_window: str | None = Field(default=None, max_length=100)
    application_deadline: str | None = Field(default=None, max_length=100)


class RequirementType(StrEnum):
    HARD = "hard"  # 硬性要求
    PREFERRED = "preferred"  # 加分项


class ExtractedRequirement(_ModelOutput):
    type: RequirementType
    text: str = Field(min_length=1, max_length=300)
    source_quote: str = Field(min_length=1, max_length=500)


class ExtractedDuty(_ModelOutput):
    text: str = Field(min_length=1, max_length=300)
    source_quote: str = Field(min_length=1, max_length=500)


class JobExtraction(_ModelOutput):
    basic_fields: BasicFields
    requirements: list[ExtractedRequirement] = Field(max_length=80)
    duties: list[ExtractedDuty] = Field(max_length=40)
    keywords: list[str] = Field(max_length=60)


def parse_job_output(completion: ChatCompletion) -> JobExtraction:
    """校验并解析模型输出；作为网关的 validate 回调时，返回值不会被使用。"""
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError("JD 解析的输出超过长度上限，被截断")
    return JobExtraction.model_validate_json(choice.message.content or "")


# ---------- 解析结果：片段经过程序校验 ----------


class Requirement(BaseModel):
    """一条要求。requirement_id 供匹配报告引用；片段位置按 Python 字符计数。"""

    requirement_id: UUID = Field(default_factory=uuid4)
    type: RequirementType
    text: str
    source_quote: str | None = None  # 用户自己加的要求没有片段
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool = False


class Duty(BaseModel):
    text: str
    source_quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool = False


class ParsedJob(BaseModel):
    basic_fields: BasicFields
    requirements: list[Requirement]
    duties: list[Duty]
    keywords: list[str]


def _locate(raw_text: str, quote: str) -> dict[str, object]:
    """在原文中找片段；出现多处取第一处。找不到时位置为空、标为未通过。"""
    spans = locate_quote(raw_text, quote)
    if not spans:
        return {"source_quote": quote, "quote_verified": False}
    start, end = spans[0]
    return {
        "source_quote": raw_text[start:end],
        "span_start": start,
        "span_end": end,
        "quote_verified": True,
    }


def build_parsed_job(raw_text: str, extraction: JobExtraction) -> ParsedJob:
    """校验每条片段、标准化关键词。纯函数，便于不调用模型直接测试。"""
    return ParsedJob(
        basic_fields=extraction.basic_fields,
        requirements=[
            Requirement(type=r.type, text=r.text, **_locate(raw_text, r.source_quote))
            for r in extraction.requirements
        ],
        duties=[
            Duty(text=d.text, **_locate(raw_text, d.source_quote))
            for d in extraction.duties
        ],
        keywords=normalize_keywords(extraction.keywords),
    )


async def parse_job(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    budget: ModelBudget,
    raw_text: str,
) -> ParsedJob:
    """调用模型解析 JD。raw_text 是规范化后的正文；调用前需已提交所属 ai_run。"""
    prompt = load_prompt("parse_jd")
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": wrap_untrusted_text("jd", raw_text)},
    ]
    completion = await request_completion(
        session_factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.PARSE_JD,
        prompt_version=prompt.label,
        messages=messages,
        json_output=True,
        budget=budget,
        validate=parse_job_output,
    )
    return build_parsed_job(raw_text, parse_job_output(completion))
