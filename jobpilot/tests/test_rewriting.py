"""改写与修订的单次调用：假上游返回预设输出，网关、标签换回和校验都真实执行。"""

import json
import re
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import AiRun, BulletSection, FactSource, Responsibility, User
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.grounding import (
    GroundingFact,
    GroundingIssue,
    IssueKind,
    check_grounding,
)
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.rewriting import (
    DraftBullet,
    RawBullet,
    RawRewrite,
    RewriteTask,
    labels_of,
    revise_bullet,
    rewrite_section,
)
from jobpilot.tests.test_call_accounting import (
    calls_of,
    completion_response,
    fake_upstream,
)

Factory = async_sessionmaker[AsyncSession]
INTERN = "云栈物流科技 后端开发实习"


def fact(text: str, responsibility=Responsibility.OWN, experience=INTERN):
    return GroundingFact(
        fact_id=uuid4(),
        experience=experience,
        responsibility=responsibility,
        text=text,
        source=FactSource.RESUME,
        quote_verified=True,
    )


F1 = fact("负责运单查询接口")
F2 = fact("使用 Go 和 Redis 缓存热点运单")
F3 = fact("运单查询 P95 耗时由 400 毫秒降至 260 毫秒", Responsibility.PARTICIPATE)
FACTS = [F1, F2, F3]


def task(**overrides) -> RewriteTask:
    values = {
        "section": BulletSection.EXPERIENCE,
        "experience": INTERN,
        "facts": FACTS,
        "focus_requirements": ["熟悉 Redis"],
        "job_title": "后端开发实习生",
        "keywords": ["Redis"],
        "max_bullets": 3,
    } | overrides
    return RewriteTask(**values)


@pytest.fixture
async def run(session_factory: Factory) -> AiRun:
    async with session_factory.begin() as session:
        owner = User(email="rewrite@example.invalid", hashed_password="test")
        session.add(owner)
        await session.flush()
        run = AiRun(user_id=owner.user_id, kind="tailor")
        session.add(run)
    return run


def output(value: dict, **kwargs) -> httpx.Response:
    return completion_response(json.dumps(value, ensure_ascii=False), **kwargs)


async def call_rewrite(session_factory, run, *responses, **task_overrides):
    pending, requests = list(responses), []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return pending.pop(0)

    async with fake_upstream(respond) as client:
        bullets = await rewrite_section(
            session_factory,
            run.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=5, remaining_tokens=50_000),
            task=task(**task_overrides),
        )
    return bullets, requests


def user_content(request: httpx.Request) -> str:
    return json.loads(request.content)["messages"][1]["content"]


# ---------- 改写 ----------


async def test_labels_are_mapped_back_to_fact_ids(
    session_factory: Factory, run: AiRun
) -> None:
    raw = {
        "bullets": [
            {
                "text": "负责运单查询接口，使用 Go 和 Redis 缓存热点运单",
                "fact_ids": ["f1", "f2", "f2"],
            },
            {"text": "运单查询 P95 耗时由 400 毫秒降至 260 毫秒", "fact_ids": ["f3"]},
        ]
    }
    bullets, _ = await call_rewrite(session_factory, run, output(raw))

    assert bullets == [
        DraftBullet(raw["bullets"][0]["text"], (F1.fact_id, F2.fact_id)),  # 重复去掉
        DraftBullet(raw["bullets"][1]["text"], (F3.fact_id,)),
    ]
    [call] = await calls_of(session_factory, run)
    assert (call.purpose, call.prompt_version) == ("rewrite", "rewrite_bullets@1")


async def test_request_carries_only_this_section(
    session_factory: Factory, run: AiRun
) -> None:
    raw = {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f1"]}]}
    _, [request] = await call_rewrite(session_factory, run, output(raw))
    content = user_content(request)

    for label in ("task", "job_focus", "facts"):
        assert f'<untrusted_data label="{label}">' in content
    assert "熟悉 Redis" in content and F3.text in content
    # 不给真实 ID 和来源；同一段经历的事实不重复写经历名（经历名在 task 区块里）
    for absent in (str(F1.fact_id), "source"):
        assert absent not in content
    [facts_block] = re.findall(_BLOCK.format("facts"), content, re.S)
    assert "experience" not in facts_block


async def test_skills_section_tells_which_experience_each_fact_is_from(
    session_factory: Factory, run: AiRun
) -> None:
    other = fact("熟悉 Python", Responsibility.NA, experience="专业技能")
    raw = {"bullets": [{"text": "Python、Go、Redis", "fact_ids": ["f1", "f2"]}]}
    _, [request] = await call_rewrite(
        session_factory,
        run,
        output(raw),
        section=BulletSection.SKILLS,
        experience=None,
        facts=[other, F2],
    )
    content = user_content(request)
    assert '"section": "skills"' in content
    assert (
        '"experience": "专业技能"' in content and f'"experience": "{INTERN}"' in content
    )


async def test_extra_bullets_beyond_limit_are_dropped(
    session_factory: Factory, run: AiRun
) -> None:
    raw = {"bullets": [{"text": f"第 {i} 条", "fact_ids": ["f1"]} for i in range(5)]}
    bullets, _ = await call_rewrite(session_factory, run, output(raw), max_bullets=2)
    assert [bullet.text for bullet in bullets] == ["第 0 条", "第 1 条"]


async def test_unknown_label_is_repaired_once(
    session_factory: Factory, run: AiRun
) -> None:
    invented = {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f9"]}]}
    fixed = {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f1"]}]}
    bullets, requests = await call_rewrite(
        session_factory, run, output(invented), output(fixed)
    )

    assert bullets == [DraftBullet("负责运单查询接口", (F1.fact_id,))]
    assert "f9" in user_content(requests[1]) or len(requests[1].content) > len(
        requests[0].content
    )  # 修复请求附上了错误
    versions = {call.prompt_version for call in await calls_of(session_factory, run)}
    assert versions == {"rewrite_bullets@1", "rewrite_bullets@1+repair_json@1"}


@pytest.mark.parametrize(
    "bad",
    [
        {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f9"]}]},
        {"bullets": [{"text": "负责运单查询接口", "fact_ids": []}]},
        {"bullets": [{"text": "   ", "fact_ids": ["f1"]}]},
        {"bullets": []},
        {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f1"], "note": "x"}]},
    ],
)
async def test_still_invalid_after_repair_raises(
    session_factory: Factory, run: AiRun, bad: dict
) -> None:
    with pytest.raises(ValidationError):
        await call_rewrite(session_factory, run, output(bad), output(bad))


async def test_truncated_output_is_not_repaired(
    session_factory: Factory, run: AiRun
) -> None:
    raw = {"bullets": [{"text": "负责运单查询接口", "fact_ids": ["f1"]}]}
    with pytest.raises(TruncatedOutputError):
        await call_rewrite(session_factory, run, output(raw, finish_reason="length"))


async def test_no_facts_means_no_call(session_factory: Factory, run: AiRun) -> None:
    bullets, requests = await call_rewrite(session_factory, run, facts=[])
    assert bullets == [] and requests == []


# ---------- 修订 ----------


async def test_revision_uses_labels_and_issue_list(
    session_factory: Factory, run: AiRun
) -> None:
    bullet = DraftBullet("主导运单查询接口开发", (F1.fact_id,))
    issue = GroundingIssue(
        kind=IssueKind.ROLE, message="「主导」高于引用事实的职责程度", fragment="主导"
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return output({"text": "负责运单查询接口开发", "fact_ids": ["f1"]})

    async with fake_upstream(respond) as client:
        revised = await revise_bullet(
            session_factory,
            run.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=3, remaining_tokens=20_000),
            bullet=bullet,
            facts=FACTS,
            issues=[issue],
        )

    assert revised == DraftBullet("负责运单查询接口开发", (F1.fact_id,))
    content = user_content(requests[0])
    assert '"fact_ids": ["f1"]' in content
    [issues_block] = re.findall(_BLOCK.format("issues"), content, re.S)
    assert json.loads(issues_block) == ["「主导」高于引用事实的职责程度"]
    assert str(F1.fact_id) not in content


# ---------- 提示词示例要过程序的检查 ----------

_BLOCK = r'<untrusted_data label="{}">\n(.*?)\n</untrusted_data>'


def _example(name: str) -> tuple[str, str]:
    text = load_prompt(name).text.split("## 示例", 1)[1]
    output_json = text.split("输出：\n", 1)[1].strip()
    return text, output_json


def _example_facts(text: str) -> dict[str, GroundingFact]:
    [facts_json] = re.findall(_BLOCK.format("facts"), text, re.S)
    return {
        row["id"]: fact(row["text"], Responsibility(row["responsibility"]))
        for row in json.loads(facts_json)
    }


def test_rewrite_example_passes_program_checks() -> None:
    """示例里的条目自己就不能编造，否则模型会照着学。"""
    text, output_json = _example("rewrite_bullets")
    facts = _example_facts(text)
    example = RawRewrite.model_validate_json(output_json, context={"labels": facts})
    available = {f.fact_id: f for f in facts.values()}
    for bullet in example.bullets:
        result = check_grounding(
            bullet.text,
            [facts[label].fact_id for label in bullet.fact_ids],
            available,
            section=BulletSection.EXPERIENCE,
        )
        assert result.passed, (bullet.text, result.issues)


def test_revise_example_fixes_what_the_checker_flags() -> None:
    text, output_json = _example("revise_bullet")
    facts = _example_facts(text)
    available = {f.fact_id: f for f in facts.values()}
    [bullet_json] = re.findall(_BLOCK.format("bullet"), text, re.S)
    before = json.loads(bullet_json)
    after = RawBullet.model_validate_json(output_json, context={"labels": facts})

    def check(sentence: str, labels: list[str]):
        return check_grounding(
            sentence,
            [facts[label].fact_id for label in labels],
            available,
            section=BulletSection.EXPERIENCE,
        )

    assert not check(before["text"], before["fact_ids"]).passed
    assert check(after.text, after.fact_ids).passed


def test_labels_follow_fact_order() -> None:
    assert labels_of(FACTS) == {"f1": F1.fact_id, "f2": F2.fact_id, "f3": F3.fact_id}
