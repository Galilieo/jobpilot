"""改写计划：分块、段落类型推断、顺序、要突出的要求和底稿判断。纯函数，不连数据库。"""

from uuid import uuid4

import pytest

from jobpilot.db.models import (
    BulletSection,
    FactKind,
    FactSource,
    Responsibility,
    SectionKind,
)
from jobpilot.services.grounding import GroundingFact
from jobpilot.services.jobs import Requirement, RequirementType
from jobpilot.services.matching import MatchItem, MatchVerdict
from jobpilot.services.tailoring_plan import (
    SKILLS_KEY,
    BaseDocStatus,
    TailorFact,
    UnresolvedSectionsError,
    base_doc_status,
    build_plan,
    resolve_sections,
)

DOC_A, DOC_B = uuid4(), uuid4()
INTERN = "云栈物流科技 后端开发实习"
INTERN2 = "星桥数据 数据开发实习"
PROJECT = "校园二手书交易小程序"
SCHOOL = "澄湖大学 计算机科学与技术 本科"
SKILLS = "技能"


def fact(
    text: str,
    experience: str = INTERN,
    section: SectionKind | None = SectionKind.INTERNSHIP,
    *,
    kind: FactKind = FactKind.ACTION,
    source: FactSource = FactSource.RESUME,
    doc=DOC_A,
    quote: str | None = None,
    verified: bool = True,
) -> TailorFact:
    return TailorFact(
        grounding=GroundingFact(
            fact_id=uuid4(),
            experience=experience,
            responsibility=Responsibility.OWN,
            text=text,
            source=source,
            quote_verified=verified and source is FactSource.RESUME,
            section=section,
        ),
        kind=kind,
        source_doc_id=doc if source is FactSource.RESUME else None,
        source_quote=(quote or text) if source is FactSource.RESUME else None,
    )


def requirement(text: str, kind=RequirementType.HARD) -> Requirement:
    return Requirement(type=kind, text=text)


def item(req: Requirement, *facts: TailorFact, verdict=MatchVerdict.COVERED):
    return MatchItem(
        requirement_id=req.requirement_id,
        verdict=verdict,
        fact_ids=[f.fact_id for f in facts],
        reason="",
        suggestion="",
    )


# ---------- 段落类型 ----------


def test_section_of_legacy_fact_is_inferred_from_its_experience() -> None:
    facts = [fact("负责运单查询接口"), fact("补充告警规则", section=None)]
    assert resolve_sections(facts) == {INTERN: SectionKind.INTERNSHIP}


@pytest.mark.parametrize(
    "sections",
    [
        [None, None],  # 一条都没有段落类型（旧版本或用户补充）
        [SectionKind.INTERNSHIP, SectionKind.PROJECT],  # 两种对不上
    ],
)
def test_unresolvable_section_needs_the_user(sections) -> None:
    facts = [fact("负责运单查询接口", section=s) for s in sections]
    with pytest.raises(UnresolvedSectionsError) as error:
        resolve_sections(facts)
    assert error.value.experiences == (INTERN,)

    overrides = {INTERN: SectionKind.PROJECT}
    assert resolve_sections(facts, overrides) == overrides


# ---------- 底稿里写了没有 ----------


def test_fact_from_the_base_doc_is_written() -> None:
    cited = [fact("用 Redis 缓存热点运单", doc=DOC_B)]
    assert (
        base_doc_status(cited, base_doc_id=DOC_B, base_text="与本句无关")
        is BaseDocStatus.WRITTEN
    )


def test_fact_from_another_resume_missing_in_base_is_suggested() -> None:
    """评审的例子：A 写了 Redis，B 为控制篇幅删掉了；用 B 投时应建议补回。"""
    cited = [fact("用 Redis 缓存热点运单", doc=DOC_A)]
    status = base_doc_status(cited, base_doc_id=DOC_B, base_text="负责运单查询接口")
    assert status is BaseDocStatus.SUGGEST_ADD


def test_text_found_in_base_counts_as_written_whatever_its_source() -> None:
    other_resume = fact("用 Go 和 Redis 缓存热点运单", doc=DOC_A)
    user_added = fact("用 Go 和 Redis 缓存热点运单", source=FactSource.USER)
    base = "实习期间用Go和Redis缓存热点运单。"  # 空白不同也算
    for cited in (other_resume, user_added):
        assert (
            base_doc_status([cited], base_doc_id=DOC_B, base_text=base)
            is BaseDocStatus.WRITTEN
        )


def test_user_fact_not_found_verbatim_is_to_check() -> None:
    """用户补充的事实可能换了说法写进底稿，程序判断不了，不冒充已写到或建议补入。"""
    cited = [
        fact("用 Redis 缓存热点运单", doc=DOC_A),
        fact("压测 QPS 到 2000", source=FactSource.USER),
    ]
    status = base_doc_status(cited, base_doc_id=DOC_B, base_text="负责运单查询接口")
    assert status is BaseDocStatus.TO_CHECK


# ---------- 计划 ----------

API = fact("负责运单查询接口")
REDIS = fact("使用 Go 和 Redis 缓存热点运单", kind=FactKind.TECHNOLOGY)
SPARK = fact(
    "用 Spark 清洗日志", INTERN2, kind=FactKind.TECHNOLOGY, doc=DOC_B
)  # fmt: skip
BOOK = fact("用 Flask 实现订单接口", PROJECT, SectionKind.PROJECT)
PY = fact("熟悉 Python", SKILLS, SectionKind.SKILL, kind=FactKind.TECHNOLOGY)
COURSE = fact("主修操作系统", SCHOOL, SectionKind.EDUCATION)
FACTS = [API, REDIS, BOOK, SPARK, PY, COURSE]

R_REDIS = requirement("熟悉 Redis")
R_SPARK = requirement("熟悉 Spark")
R_FLASK = requirement("了解 Flask", RequirementType.PREFERRED)
R_K8S = requirement("熟悉 Kubernetes")
REQS = [R_REDIS, R_SPARK, R_FLASK, R_K8S]
ITEMS = [
    item(R_REDIS, REDIS),
    item(R_SPARK, SPARK, verdict=MatchVerdict.PARTIAL),
    item(R_FLASK, BOOK),
    item(R_K8S, verdict=MatchVerdict.MISSING),
]


def plan(base_doc_id=DOC_A, base_text=""):
    return build_plan(FACTS, REQS, ITEMS, base_doc_id=base_doc_id, base_text=base_text)


def test_blocks_cover_rewritten_sections_and_skills_only() -> None:
    blocks = plan()
    assert [b.key for b in blocks] == [
        f"experience:{INTERN}",
        f"experience:{INTERN2}",
        f"experience:{PROJECT}",
        SKILLS_KEY,
    ]  # 教育不改写
    skills = blocks[-1]
    assert skills.bullet_section is BulletSection.SKILLS and skills.experience is None
    # 技能栏的事实，加上各段经历里的技术事实
    assert set(skills.fact_ids) == {PY.fact_id, REDIS.fact_id, SPARK.fact_id}


def test_blocks_supporting_more_requirements_come_first_within_a_section() -> None:
    extra = requirement("有日志处理经验")
    items = [*ITEMS, item(extra, SPARK)]
    blocks = build_plan(
        FACTS, [*REQS, extra], items, base_doc_id=DOC_A, base_text=""
    )  # fmt: skip
    # 第二段实习支撑两条硬性要求，排到第一段前面；项目仍在实习之后
    assert [b.experience for b in blocks[:3]] == [INTERN2, INTERN, PROJECT]


def test_focus_lists_supported_requirements_hard_first() -> None:
    intern, intern2, project, skills = plan()
    assert [f.text for f in intern.focus] == ["熟悉 Redis"]
    assert [f.text for f in intern2.focus] == ["熟悉 Spark"]  # 部分覆盖也算
    assert [f.text for f in project.focus] == ["了解 Flask"]
    assert {f.text for f in skills.focus} == {"熟悉 Redis", "熟悉 Spark"}
    all_focus = {f.text for b in plan() for f in b.focus}
    assert "熟悉 Kubernetes" not in all_focus  # 没有依据的要求不让模型去凑


def test_same_facts_and_job_with_two_base_docs_differ_only_in_base_status() -> None:
    """评审验收：同一个人、同一份 JD、两份底稿，能力结论相同，补写建议不同。"""
    with_a = plan(base_doc_id=DOC_A, base_text="")
    with_b = plan(base_doc_id=DOC_B, base_text="")

    def focus(blocks):
        return {(b.key, f.text) for b in blocks for f in b.focus}

    def status(blocks, text):
        return {f.base_status for b in blocks for f in b.focus if f.text == text}

    assert focus(with_a) == focus(with_b)
    assert status(with_a, "熟悉 Redis") == {BaseDocStatus.WRITTEN}
    assert status(with_b, "熟悉 Redis") == {BaseDocStatus.SUGGEST_ADD}
    assert status(with_a, "熟悉 Spark") == {BaseDocStatus.SUGGEST_ADD}
    assert status(with_b, "熟悉 Spark") == {BaseDocStatus.WRITTEN}


def test_within_same_type_requirements_missing_from_base_come_first() -> None:
    _, _, _, skills = plan(base_doc_id=DOC_B, base_text="")
    # 两条都是硬性要求；Redis 不在底稿 B 里，排在已写到的 Spark 前面
    assert [f.text for f in skills.focus] == ["熟悉 Redis", "熟悉 Spark"]
    _, _, _, skills = plan(base_doc_id=DOC_A, base_text="")
    assert [f.text for f in skills.focus] == ["熟悉 Spark", "熟悉 Redis"]
