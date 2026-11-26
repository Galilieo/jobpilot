"""改写与修订：一次模型调用写出一段经历（或技能概述）的条目，或者按问题清单改一条。

每次只放入这段经历的事实和这段要突出的岗位要求（02 第 11.3 节），不放整份事实库和 JD。
事实 ID 不直接给模型：用 f1、f2 这样的短标签，程序再换回真实 ID。
短标签省 token，也避免模型抄错 UUID；
模型编出不存在的标签时按输出不合格处理，网关会修复一次。

这里只负责「写」和「校验输出的格式与引用范围」，内容有没有超出事实由依据检查负责。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.db.models import BulletSection
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.grounding import GroundingFact, GroundingIssue
from jobpilot.services.llm_gateway import (
    ModelQuota,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt

MAX_BULLET_CHARS = 120  # 提示词要求 60 字以内；这里放宽一倍，只拦明显失控的输出


@dataclass(frozen=True)
class RewriteTask:
    """一次改写调用的输入：一段经历，或技能概述。"""

    section: BulletSection
    experience: str | None  # 技能概述为 None
    facts: Sequence[GroundingFact]
    focus_requirements: Sequence[str]  # 这一段要突出的岗位要求
    job_title: str | None = None
    keywords: Sequence[str] = ()
    max_bullets: int = 4


@dataclass(frozen=True)
class DraftBullet:
    """模型写出、已换回真实事实 ID 的一条，还没经过依据检查。"""

    text: str
    fact_ids: tuple[UUID, ...]


def labels_of(facts: Sequence[GroundingFact]) -> dict[str, UUID]:
    """按顺序给事实编短标签 f1、f2……"""
    return {f"f{index}": fact.fact_id for index, fact in enumerate(facts, start=1)}


class RawBullet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_BULLET_CHARS)
    fact_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text 不能为空白")
        return value.strip()

    @field_validator("fact_ids")
    @classmethod
    def known_labels(cls, value: list[str], info: ValidationInfo) -> list[str]:
        # 校验时由调用方传入本次给模型看的标签；不在里面的就是模型编出来的。
        allowed = (info.context or {}).get("labels")
        if allowed is not None:
            unknown = [label for label in value if label not in allowed]
            if unknown:
                raise ValueError(f"引用了不存在的事实：{', '.join(unknown)}")
        return list(dict.fromkeys(value))


class RawRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bullets: list[RawBullet] = Field(min_length=1, max_length=10)


def _content(completion: ChatCompletion, what: str) -> str:
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError(f"{what}的输出超过长度上限，被截断")
    return choice.message.content or ""


def parse_rewrite(completion: ChatCompletion, labels: dict[str, UUID]) -> RawRewrite:
    return RawRewrite.model_validate_json(
        _content(completion, "改写"), context={"labels": labels}
    )


def parse_revision(completion: ChatCompletion, labels: dict[str, UUID]) -> RawBullet:
    return RawBullet.model_validate_json(
        _content(completion, "修订"), context={"labels": labels}
    )


def _to_draft(raw: RawBullet, labels: dict[str, UUID]) -> DraftBullet:
    return DraftBullet(raw.text, tuple(labels[label] for label in raw.fact_ids))


def _facts_payload(facts: Sequence[GroundingFact]) -> str:
    """只给模型 id、职责和事实文字：不给来源、原文片段和真实 ID。"""
    # 技能概述跨经历，要让模型知道每条事实出自哪段经历。
    several = len({fact.experience for fact in facts}) > 1
    rows = [
        {
            "id": label,
            "responsibility": fact.responsibility.value,
            "text": fact.text,
            **({"experience": fact.experience} if several else {}),
        }
        for label, fact in zip(labels_of(facts), facts, strict=True)
    ]
    return json.dumps(rows, ensure_ascii=False)


def format_rewrite_messages(
    task: RewriteTask, *, system_prompt: str
) -> list[ChatCompletionMessageParam]:
    task_payload: dict[str, Any] = {
        "section": task.section.value,
        "max_bullets": task.max_bullets,
    }
    if task.experience is not None:
        task_payload["experience"] = task.experience
    focus = {
        "title": task.job_title,
        "requirements": list(task.focus_requirements),
        "keywords": list(task.keywords),
    }
    user_content = "\n\n".join(
        [
            wrap_untrusted_text("task", json.dumps(task_payload, ensure_ascii=False)),
            wrap_untrusted_text("job_focus", json.dumps(focus, ensure_ascii=False)),
            wrap_untrusted_text("facts", _facts_payload(task.facts)),
        ]
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def format_revise_messages(
    bullet: DraftBullet,
    facts: Sequence[GroundingFact],
    issues: Sequence[GroundingIssue],
    *,
    system_prompt: str,
) -> list[ChatCompletionMessageParam]:
    by_id = {fact_id: label for label, fact_id in labels_of(facts).items()}
    bullet_payload = {
        "text": bullet.text,
        "fact_ids": [by_id[fact_id] for fact_id in bullet.fact_ids if fact_id in by_id],
    }
    user_content = "\n\n".join(
        [
            wrap_untrusted_text(
                "bullet", json.dumps(bullet_payload, ensure_ascii=False)
            ),
            wrap_untrusted_text("facts", _facts_payload(facts)),
            # 问题清单里带着模型自己写的片段，也按不可信数据处理。
            wrap_untrusted_text(
                "issues",
                json.dumps([issue.message for issue in issues], ensure_ascii=False),
            ),
        ]
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


async def rewrite_section(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    budget: ModelQuota,
    task: RewriteTask,
) -> list[DraftBullet]:
    """一次调用写出一段经历（或技能概述）的条目。调用前需已提交所属 ai_run。"""
    if not task.facts:
        return []
    prompt = load_prompt("rewrite_bullets")
    labels = labels_of(task.facts)
    completion = await request_completion(
        session_factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.REWRITE,
        prompt_version=prompt.label,
        messages=format_rewrite_messages(task, system_prompt=prompt.text),
        json_output=True,
        budget=budget,
        validate=lambda completion: parse_rewrite(completion, labels),
    )
    raw = parse_rewrite(completion, labels)
    return [_to_draft(bullet, labels) for bullet in raw.bullets[: task.max_bullets]]


async def revise_bullet(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    budget: ModelQuota,
    bullet: DraftBullet,
    facts: Sequence[GroundingFact],
    issues: Sequence[GroundingIssue],
) -> DraftBullet:
    """按依据检查的问题清单改一条。facts 是这一条可以引用的全部事实。"""
    prompt = load_prompt("revise_bullet")
    labels = labels_of(facts)
    completion = await request_completion(
        session_factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.REWRITE,
        prompt_version=prompt.label,
        messages=format_revise_messages(
            bullet, facts, issues, system_prompt=prompt.text
        ),
        json_output=True,
        budget=budget,
        validate=lambda completion: parse_revision(completion, labels),
    )
    return _to_draft(parse_revision(completion, labels), labels)
