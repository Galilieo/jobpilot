"""改写一块：通过、修订后通过、两次修订仍不过而回退、预算用完、调用失败。

假上游按收到的提示词分辨是改写、修订还是第三层检查，网关、标签换回和三层检查都真实执行。
"""

import asyncio
import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    BulletSection,
    CheckStatus,
    FactSource,
    Responsibility,
    SectionKind,
    User,
)
from jobpilot.services.grounding import GroundingFact, IssueKind, check_grounding
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.rewriting import DraftBullet
from jobpilot.services.tailoring import (
    BlockStatus,
    WriteContext,
    fallback,
    write_block,
)
from jobpilot.services.tailoring_plan import PlanBlock
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream

Factory = async_sessionmaker[AsyncSession]
INTERN = "云栈物流科技 后端开发实习"
OTHER = "星桥数据 数据开发实习"


def fact(text, responsibility=Responsibility.OWN, experience=INTERN, verified=True):
    return GroundingFact(
        fact_id=uuid4(),
        experience=experience,
        responsibility=responsibility,
        text=text,
        source=FactSource.RESUME,
        quote_verified=verified,
        section=SectionKind.INTERNSHIP,
    )


F1 = fact("负责运单查询接口")
F2 = fact("使用 Go 和 Redis 缓存热点运单")
F3 = fact("运单查询 P95 耗时由 400 毫秒降至 260 毫秒", Responsibility.PARTICIPATE)
ELSEWHERE = fact("用 Spark 清洗日志", experience=OTHER)
AVAILABLE = {f.fact_id: f for f in (F1, F2, F3, ELSEWHERE)}
BLOCK = PlanBlock(
    key=f"experience:{INTERN}",
    section=SectionKind.INTERNSHIP,
    bullet_section=BulletSection.EXPERIENCE,
    experience=INTERN,
    fact_ids=(F1.fact_id, F2.fact_id, F3.fact_id),
    focus=(),
)

SUPPORTED = {"verdict": "supported", "unsupported_spans": []}


def partial(span: str) -> dict:
    return {"verdict": "partial", "unsupported_spans": [{"text": span, "reason": "无"}]}


def bullet(text: str, *labels: str) -> dict:
    return {"text": text, "fact_ids": list(labels)}


class Upstream:
    """按提示词分流：rewrite、revise、check 各自按顺序取预设回复。"""

    _PROMPTS = {
        "rewrite": "rewrite_bullets",
        "revise": "revise_bullet",
        "check": "ground_check",
    }

    def __init__(self, **replies: list[dict | Callable[[], httpx.Response]]) -> None:
        self.replies = {kind: list(values) for kind, values in replies.items()}
        self.seen: dict[str, int] = {kind: 0 for kind in self._PROMPTS}
        self._systems = {
            kind: load_prompt(name).text for kind, name in self._PROMPTS.items()
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        kind = next(k for k, text in self._systems.items() if system == text)
        self.seen[kind] += 1
        reply = self.replies[kind].pop(0)
        if callable(reply):
            return reply()
        return completion_response(json.dumps(reply, ensure_ascii=False))


@pytest.fixture
async def run(session_factory: Factory) -> AiRun:
    async with session_factory.begin() as session:
        owner = User(email="tailor@example.invalid", hashed_password="test")
        session.add(owner)
        await session.flush()
        run = AiRun(user_id=owner.user_id, kind="tailor")
        session.add(run)
    return run


async def write(session_factory, run, upstream, *, calls=20, block=BLOCK):
    async with fake_upstream(upstream) as client:
        ctx = WriteContext(
            session_factory=session_factory,
            user_id=run.user_id,
            run_id=run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=calls, remaining_tokens=200_000),
            limiter=asyncio.Semaphore(1),  # 一次一个请求，顺序可预测
            available=AVAILABLE,
            known_organizations=frozenset({"云栈物流科技", "星桥数据"}),
            job_title="后端开发实习生",
            keywords=("Redis",),
        )
        return await write_block(ctx, block)


async def test_all_bullets_pass(session_factory: Factory, run: AiRun) -> None:
    upstream = Upstream(
        rewrite=[
            {
                "bullets": [
                    bullet(
                        "负责运单查询接口，使用 Go 和 Redis 缓存热点运单", "f1", "f2"
                    ),
                    bullet("运单查询 P95 耗时由 400 毫秒降至 260 毫秒", "f3"),
                ]
            }
        ],
        check=[SUPPORTED, SUPPORTED],
    )
    outcome = await write(session_factory, run, upstream)

    assert outcome.status is BlockStatus.DONE
    assert [(b.check_status, b.revision_count) for b in outcome.bullets] == [
        (CheckStatus.PASSED, 0),
        (CheckStatus.PASSED, 0),
    ]
    assert outcome.bullets[0].fact_ids == (F1.fact_id, F2.fact_id)
    assert upstream.seen == {"rewrite": 1, "revise": 0, "check": 2}


async def test_program_issue_is_revised_before_paying_for_layer_three(
    session_factory: Factory, run: AiRun
) -> None:
    upstream = Upstream(
        rewrite=[{"bullets": [bullet("主导运单查询接口", "f1")]}],
        revise=[bullet("负责运单查询接口", "f1")],
        check=[SUPPORTED],
    )
    outcome = await write(session_factory, run, upstream)

    [only] = outcome.bullets
    assert (only.text, only.check_status, only.revision_count) == (
        "负责运单查询接口",
        CheckStatus.PASSED,
        1,
    )
    # 「主导」第二层就拦下，第一次检查不调第三层；修订后才调一次
    assert upstream.seen == {"rewrite": 1, "revise": 1, "check": 1}


async def test_layer_three_issue_is_revised(
    session_factory: Factory, run: AiRun
) -> None:
    upstream = Upstream(
        rewrite=[{"bullets": [bullet("负责运单查询接口，显著提升稳定性", "f1")]}],
        revise=[bullet("负责运单查询接口", "f1")],
        check=[partial("显著提升稳定性"), SUPPORTED],
    )
    outcome = await write(session_factory, run, upstream)
    [only] = outcome.bullets
    assert (only.check_status, only.revision_count) == (CheckStatus.PASSED, 1)


async def test_still_failing_after_two_revisions_falls_back_to_fact_text(
    session_factory: Factory, run: AiRun
) -> None:
    stubborn = bullet("主导运单查询接口，使用 Go 和 Redis 缓存热点运单", "f1", "f2")
    upstream = Upstream(
        rewrite=[{"bullets": [stubborn]}],
        revise=[stubborn, stubborn],
        check=[partial("主导")],  # 最后一次修订之前，第三层也一起查，问题一次给全
    )
    outcome = await write(session_factory, run, upstream)

    [only] = outcome.bullets
    assert only.check_status is CheckStatus.FALLBACK
    assert only.text == "负责运单查询接口；使用 Go 和 Redis 缓存热点运单"
    assert only.revision_count == 2
    assert IssueKind.ROLE in {issue.kind for issue in only.issues}
    assert upstream.seen == {"rewrite": 1, "revise": 2, "check": 1}
    # 回退的文字本身要能通过第一、二层
    assert check_grounding(
        only.text,
        only.fact_ids,
        AVAILABLE,
        section=BulletSection.EXPERIENCE,
        experience=INTERN,
    ).passed


async def test_revision_call_failure_falls_back(
    session_factory: Factory, run: AiRun
) -> None:
    broken = {"text": "负责运单查询接口"}  # 缺 fact_ids，修复一次后仍不合格
    upstream = Upstream(
        rewrite=[{"bullets": [bullet("主导运单查询接口", "f1")]}],
        revise=[broken, broken],
    )
    outcome = await write(session_factory, run, upstream)
    [only] = outcome.bullets
    assert (only.text, only.check_status, only.revision_count) == (
        F1.text,
        CheckStatus.FALLBACK,
        0,
    )


def test_fallback_without_usable_fact_is_pending() -> None:
    unverified = fact("负责运单查询接口", verified=False)
    available = {unverified.fact_id: unverified, ELSEWHERE.fact_id: ELSEWHERE}
    draft = DraftBullet("主导接口", (unverified.fact_id, ELSEWHERE.fact_id))
    result = fallback(BLOCK, available, draft, (), 2)
    # 未通过原文校验的不用；别的经历的事实也不能回退进这段经历
    assert result.check_status is CheckStatus.PENDING
    assert result.text == "主导接口"


async def test_budget_running_out_keeps_settled_bullets_only(
    session_factory: Factory, run: AiRun
) -> None:
    upstream = Upstream(
        rewrite=[
            {
                "bullets": [
                    bullet("负责运单查询接口", "f1"),
                    bullet("使用 Go 和 Redis 缓存热点运单", "f2"),
                ]
            }
        ],
        check=[SUPPORTED],
    )
    # 改写 1 次 + 第一条的第三层 1 次；第二条要查第三层时额度已用完
    outcome = await write(session_factory, run, upstream, calls=2)

    assert outcome.status is BlockStatus.INTERRUPTED
    assert [b.text for b in outcome.bullets] == ["负责运单查询接口"]
    assert upstream.seen == {"rewrite": 1, "revise": 0, "check": 1}


async def test_budget_out_before_rewriting_leaves_block_empty(
    session_factory: Factory, run: AiRun
) -> None:
    outcome = await write(session_factory, run, Upstream(), calls=0)
    assert (outcome.status, outcome.bullets) == (BlockStatus.INTERRUPTED, ())


async def test_rewrite_call_failure_marks_block_failed(
    session_factory: Factory, run: AiRun
) -> None:
    def server_error() -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    upstream = Upstream(rewrite=[server_error])
    outcome = await write(session_factory, run, upstream)
    assert (outcome.status, outcome.bullets) == (BlockStatus.FAILED, ())
