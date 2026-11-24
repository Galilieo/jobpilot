"""改写流程端到端（假模型，真实数据库）：全部通过、沿用匹配报告、预算用完、匹配失败、
运行已被别人结束。查询结果只从业务表读。
"""

import asyncio
import json
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    BulletFact,
    BulletSection,
    CheckStatus,
    Fact,
    FactSet,
    JobPosting,
    MatchReport,
    ModelCall,
    ResumeDocument,
    RunStatus,
    SectionKind,
    TailoredBullet,
    User,
    UserJob,
)
from jobpilot.db.runs import RunAlreadyFinishedError, set_run_status
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.tailoring import BlockOutcome, BlockStatus, FinalBullet
from jobpilot.services.tailoring_graph import TailorDeps, run_tailoring
from jobpilot.services.tailoring_plan import PlanBlock
from jobpilot.services.tailoring_store import load_inputs, save_outcome
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream

Factory = async_sessionmaker[AsyncSession]

INTERN = "云栈物流科技 后端开发实习"
PROJECT = "校园二手书交易小程序"
SKILLS = "技能"
# (经历, 段落类型, kind, 文字)
FACT_ROWS = [
    (INTERN, "internship", "action", "负责运单查询接口"),
    (INTERN, "internship", "technology", "使用 Go 和 Redis 缓存热点运单"),
    (PROJECT, "project", "technology", "用 Flask 实现订单接口"),
    (SKILLS, "skill", "technology", "熟悉 Python"),
]
RAW_TEXT = "\n".join(text for *_, text in FACT_ROWS)


SNAPSHOT = {
    "basic_fields": {"title": "后端开发实习生"},
    "requirements": [
        {"requirement_id": rid, "type": kind, "text": text}
        for rid, kind, text in [
            ("11111111-1111-4111-8111-111111111111", "hard", "熟悉 Redis"),
            ("22222222-2222-4222-8222-222222222222", "preferred", "了解 Flask"),
        ]
    ],
    "duties": [],
    "keywords": ["Redis"],
}
REQUIREMENT_IDS = [r["requirement_id"] for r in SNAPSHOT["requirements"]]


@dataclass
class SeededInputs:
    """一个用户已上传的简历、已发布的事实和收藏的岗位，还没有改写运行。"""

    doc_id: UUID
    fact_set_id: UUID
    fact_ids: list[UUID]
    user_job_id: UUID


@dataclass
class Seeded:
    user_id: UUID
    run_id: UUID
    fact_ids: list[UUID]
    requirement_ids: list[str]


async def seed_inputs(
    session_factory: Factory, user_id: UUID, *, sections: bool = True
) -> SeededInputs:
    """sections=False 时事实都没有段落类型（模拟旧版本），改写前要用户指定。"""
    async with session_factory.begin() as session:
        doc = ResumeDocument(user_id=user_id, raw_text=RAW_TEXT)
        fact_set = FactSet(user_id=user_id, version=1)
        posting = JobPosting(
            owner_id=user_id,
            raw_text="岗位原文",
            content_hash=str(user_id).replace("-", "").ljust(64, "x"),
            parsed_json=SNAPSHOT,
        )
        session.add_all([doc, fact_set, posting])
        await session.flush()
        facts = [
            Fact(
                user_id=user_id,
                fact_set_id=fact_set.fact_set_id,
                lineage_id=uuid4(),
                experience=experience,
                section=section if sections else None,
                kind=kind,
                responsibility="own" if section != "skill" else "na",
                text=text,
                has_number=False,
                source="resume",
                source_doc_id=doc.doc_id,
                source_quote=text,
                span_start=RAW_TEXT.index(text),
                span_end=RAW_TEXT.index(text) + len(text),
                quote_verified=True,
            )
            for experience, section, kind, text in FACT_ROWS
        ]
        user_job = UserJob(user_id=user_id, posting_id=posting.posting_id)
        session.add_all([*facts, user_job])
        await session.flush()
        return SeededInputs(
            doc_id=doc.doc_id,
            fact_set_id=fact_set.fact_set_id,
            fact_ids=[fact.fact_id for fact in facts],
            user_job_id=user_job.user_job_id,
        )


async def seed(session_factory: Factory, email: str) -> Seeded:
    async with session_factory.begin() as session:
        user = User(email=email, hashed_password="test")
        session.add(user)
        await session.flush()
        user_id = user.user_id
    inputs = await seed_inputs(session_factory, user_id)
    async with session_factory.begin() as session:
        run = AiRun(
            user_id=user_id,
            kind="tailor",
            user_job_id=inputs.user_job_id,
            fact_set_id=inputs.fact_set_id,
            base_doc_id=inputs.doc_id,
            job_snapshot_json=SNAPSHOT,
        )
        session.add(run)
        await session.flush()
        return Seeded(
            user_id=user_id,
            run_id=run.run_id,
            fact_ids=inputs.fact_ids,
            requirement_ids=REQUIREMENT_IDS,
        )


async def another_tailor_run(session_factory: Factory, seeded: Seeded) -> UUID:
    async with session_factory.begin() as session:
        first = await session.get_one(AiRun, seeded.run_id)
        run = AiRun(
            user_id=first.user_id,
            kind="tailor",
            user_job_id=first.user_job_id,
            fact_set_id=first.fact_set_id,
            base_doc_id=first.base_doc_id,
            job_snapshot_json=first.job_snapshot_json,
        )
        session.add(run)
        await session.flush()
        return run.run_id


# ---------- 假模型 ----------

INTERN_BULLET = "负责运单查询接口，使用 Go 和 Redis 缓存热点运单"
REWRITES = {
    INTERN: [{"text": INTERN_BULLET, "fact_ids": ["f1", "f2"]}],
    PROJECT: [{"text": "用 Flask 实现订单接口", "fact_ids": ["f1"]}],
    None: [{"text": "Go、Redis、Flask、Python", "fact_ids": ["f1", "f2", "f3"]}],
}
SUPPORTED = {"verdict": "supported", "unsupported_spans": []}
_TASK = re.compile(r'<untrusted_data label="task">\n(.*?)\n</untrusted_data>', re.S)


class FakeModel:
    """按提示词分辨调用；改写按 task 里的经历回复（各块并行，到达顺序不固定）。"""

    def __init__(self, fact_ids: list[UUID], *, match_status: int = 200) -> None:
        self.fact_ids = fact_ids  # 事实库里的四条，匹配结果按它引用
        self.match_status = match_status
        self.calls: dict[str, int] = {"match": 0, "rewrite": 0, "check": 0}
        self._kinds = {
            load_prompt("match_job").text: "match",
            load_prompt("rewrite_bullets").text: "rewrite",
            load_prompt("ground_check").text: "check",
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        kind = self._kinds[messages[0]["content"]]
        self.calls[kind] += 1
        if kind == "match":
            if self.match_status != 200:
                return httpx.Response(self.match_status, json={"error": {}})
            return self._reply(self._match())
        if kind == "rewrite":
            task = json.loads(_TASK.search(messages[1]["content"]).group(1))
            return self._reply({"bullets": REWRITES[task.get("experience")]})
        return self._reply(SUPPORTED)

    def _match(self) -> dict:
        redis, flask = REQUIREMENT_IDS
        _, cache, orders, _ = self.fact_ids
        return {
            "items": [
                {
                    "requirement_id": redis,
                    "verdict": "covered",
                    "fact_ids": [str(cache)],
                    "reason": "用过 Redis",
                    "suggestion": "保留",
                },
                {
                    "requirement_id": flask,
                    "verdict": "covered",
                    "fact_ids": [str(orders)],
                    "reason": "用过 Flask",
                    "suggestion": "保留",
                },
            ],
            "main_gaps": [],
        }

    @staticmethod
    def _reply(value: dict) -> httpx.Response:
        return completion_response(json.dumps(value, ensure_ascii=False))


async def tailor(
    session_factory: Factory,
    seeded: Seeded,
    model: FakeModel,
    *,
    run_id: UUID | None = None,
    calls: int = 30,
    concurrency: int = 4,
) -> None:
    async with fake_upstream(model) as client:
        deps = TailorDeps(
            session_factory=session_factory,
            client=client,
            budget=ModelBudget(remaining_calls=calls, remaining_tokens=500_000),
            limiter=asyncio.Semaphore(concurrency),
        )
        await run_tailoring(deps, seeded.user_id, run_id or seeded.run_id)


# ---------- 读回（新连接，只读业务表） ----------


async def read_run(session_factory: Factory, run_id: UUID) -> AiRun:
    async with session_factory() as session:
        return await session.get_one(AiRun, run_id)


async def read_bullets(session_factory: Factory, run_id: UUID):
    async with session_factory() as session:
        rows = await session.scalars(
            select(TailoredBullet)
            .where(TailoredBullet.run_id == run_id)
            .order_by(TailoredBullet.order_no)
        )
        bullets = list(rows.all())
        refs = await session.scalars(
            select(BulletFact).where(
                BulletFact.bullet_id.in_([b.bullet_id for b in bullets])
            )
        )
        return bullets, list(refs.all())


async def count(session_factory: Factory, query) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(query.subquery()))


def match_runs(user_id: UUID):
    return select(AiRun).where(AiRun.user_id == user_id, AiRun.kind == "match")


# ---------- 用例 ----------


async def test_full_run_succeeds_and_saves_bullets_in_plan_order(
    session_factory: Factory,
) -> None:
    seeded = await seed(session_factory, "full@tailor.invalid")
    model = FakeModel(seeded.fact_ids)
    await tailor(session_factory, seeded, model)

    run = await read_run(session_factory, seeded.run_id)
    assert (run.status, run.stop_reason) == (RunStatus.SUCCEEDED, None)
    assert run.finished_at is not None
    bullets, refs = await read_bullets(session_factory, seeded.run_id)
    assert [(b.order_no, b.section, b.experience, b.check_status) for b in bullets] == [
        (0, "experience", INTERN, "passed"),
        (1, "experience", PROJECT, "passed"),
        (2, "skills", None, "passed"),
    ]
    assert len(refs) == 2 + 1 + 3
    blocks = run.plan_json["blocks"]
    assert [b["status"] for b in blocks] == ["done", "done", "done"]
    # 计划里记下了底稿判断：底稿就是事实的来源，已写到
    assert blocks[0]["focus"][0]["base_status"] == "written"
    # 改写前新建了一次匹配运行和报告；每块一次改写，每条一次第三层
    assert await count(session_factory, match_runs(seeded.user_id)) == 1
    assert model.calls == {"match": 1, "rewrite": 3, "check": 3}


async def test_unchanged_inputs_reuse_the_match_report(
    session_factory: Factory,
) -> None:
    seeded = await seed(session_factory, "reuse@tailor.invalid")
    await tailor(session_factory, seeded, FakeModel(seeded.fact_ids))

    second = await another_tailor_run(session_factory, seeded)
    model = FakeModel(seeded.fact_ids)
    await tailor(session_factory, seeded, model, run_id=second)

    assert model.calls["match"] == 0
    assert await count(session_factory, match_runs(seeded.user_id)) == 1
    assert (await read_run(session_factory, second)).status == RunStatus.SUCCEEDED


async def test_changed_job_snapshot_runs_a_new_match(
    session_factory: Factory,
) -> None:
    seeded = await seed(session_factory, "changed@tailor.invalid")
    await tailor(session_factory, seeded, FakeModel(seeded.fact_ids))

    second = await another_tailor_run(session_factory, seeded)
    async with session_factory.begin() as session:
        run = await session.get_one(AiRun, second)
        run.job_snapshot_json = {**run.job_snapshot_json, "keywords": ["Go"]}
    model = FakeModel(seeded.fact_ids)
    await tailor(session_factory, seeded, model, run_id=second)

    assert model.calls["match"] == 1
    assert await count(session_factory, match_runs(seeded.user_id)) == 2


async def test_budget_running_out_mid_run_ends_budget_exhausted(
    session_factory: Factory,
) -> None:
    """各块并行，哪一块先拿到最后的额度不固定；这里只断言与调度顺序无关的结果。"""
    seeded = await seed(session_factory, "budget@tailor.invalid")
    model = FakeModel(seeded.fact_ids)
    await tailor(session_factory, seeded, model, calls=5)

    run = await read_run(session_factory, seeded.run_id)
    assert (run.status, run.stop_reason) == ("budget_exhausted", "budget_exhausted")
    assert sum(model.calls.values()) == 5  # 额度用完为止，没有多发
    statuses = {b["experience"]: b["status"] for b in run.plan_json["blocks"]}
    assert "interrupted" in statuses.values()
    bullets, _ = await read_bullets(session_factory, seeded.run_id)
    # 存下来的都是定稿的条目，属于完成或中断的块
    assert all(b.check_status == "passed" for b in bullets)
    assert all(statuses[b.experience] in {"done", "interrupted"} for b in bullets)


async def test_saved_partial_result_shows_finished_block_and_stop_position(
    session_factory: Factory,
) -> None:
    """评审验收：第一段已通过，第二段在调用前额度用完；用新连接重新读取，两者都在，
    运行没有成功。"""
    seeded = await seed(session_factory, "partial@tailor.invalid")
    inputs = await load_inputs(session_factory, seeded.user_id, seeded.run_id)
    first, second = (
        PlanBlock(
            key=f"experience:{name}",
            section=SectionKind(section),
            bullet_section=BulletSection.EXPERIENCE,
            experience=name,
            fact_ids=(),
            focus=(),
        )
        for name, section in [(INTERN, "internship"), (PROJECT, "project")]
    )
    done = BlockOutcome(
        key=first.key,
        status=BlockStatus.DONE,
        bullets=(
            FinalBullet(
                text="负责运单查询接口",
                fact_ids=(seeded.fact_ids[0],),
                check_status=CheckStatus.PASSED,
                revision_count=0,
            ),
        ),
    )
    stopped = BlockOutcome(key=second.key, status=BlockStatus.INTERRUPTED)

    status = await save_outcome(
        session_factory,
        seeded.user_id,
        seeded.run_id,
        fact_set_id=inputs.fact_set_id,
        plan=[first, second],
        outcomes=[stopped, done],  # 并行完成的顺序不影响编号
        stop_reason=None,
    )

    assert status is RunStatus.BUDGET_EXHAUSTED
    run = await read_run(session_factory, seeded.run_id)
    assert run.stop_reason == "budget_exhausted"
    assert [(b["experience"], b["status"]) for b in run.plan_json["blocks"]] == [
        (INTERN, "done"),
        (PROJECT, "interrupted"),
    ]
    bullets, refs = await read_bullets(session_factory, seeded.run_id)
    assert [(b.order_no, b.text) for b in bullets] == [(0, "负责运单查询接口")]
    assert [r.fact_id for r in refs] == [seeded.fact_ids[0]]


async def test_budget_used_up_by_match_leaves_every_block_not_started(
    session_factory: Factory,
) -> None:
    seeded = await seed(session_factory, "early@tailor.invalid")
    await tailor(session_factory, seeded, FakeModel(seeded.fact_ids), calls=1)

    run = await read_run(session_factory, seeded.run_id)
    # 匹配用掉唯一一次额度后，三块都在改写前就停下
    assert run.stop_reason == "budget_exhausted"
    assert {b["status"] for b in run.plan_json["blocks"]} == {"interrupted"}
    assert (await read_bullets(session_factory, seeded.run_id))[0] == []


async def test_failed_match_fails_the_run_without_bullets(
    session_factory: Factory,
) -> None:
    seeded = await seed(session_factory, "nomatch@tailor.invalid")
    model = FakeModel(seeded.fact_ids, match_status=400)
    await tailor(session_factory, seeded, model)

    run = await read_run(session_factory, seeded.run_id)
    assert (run.status, run.stop_reason) == (RunStatus.FAILED, "match_failed")
    assert run.plan_json == {"blocks": []}
    assert model.calls["rewrite"] == 0
    async with session_factory() as session:
        [match_run] = (await session.scalars(match_runs(seeded.user_id))).all()
    assert match_run.status == RunStatus.FAILED
    assert await count(session_factory, select(MatchReport)) == 0


async def test_run_finished_elsewhere_writes_nothing(
    session_factory: Factory,
) -> None:
    """另一个事务先把运行标成失败（例如重启回收），晚到的结果不能写进去。"""
    seeded = await seed(session_factory, "late@tailor.invalid")
    async with session_factory.begin() as session:
        await set_run_status(session, seeded.user_id, seeded.run_id, RunStatus.FAILED)

    with pytest.raises(RunAlreadyFinishedError):
        await tailor(session_factory, seeded, FakeModel(seeded.fact_ids))

    run = await read_run(session_factory, seeded.run_id)
    assert (run.status, run.stop_reason, run.plan_json) == ("failed", None, None)
    assert (await read_bullets(session_factory, seeded.run_id))[0] == []
    # 模型调用照样记账
    calls = select(ModelCall).where(ModelCall.run_id == seeded.run_id)
    assert await count(session_factory, calls) == 6
