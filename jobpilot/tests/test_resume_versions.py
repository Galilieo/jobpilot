"""保存编辑前的核对（recheck）：依据字段以上一版为准，改过的条目重新检查。纯函数。"""

from uuid import uuid4

import pytest

from jobpilot.db.models import CheckStatus, FactSource, Responsibility, SectionKind
from jobpilot.services.grounding import GroundingFact, IssueKind
from jobpilot.services.resume_content import (
    CheckLayers,
    Entry,
    ResumeContent,
    ResumeSection,
    bullet_of,
)
from jobpilot.services.resume_versions import (
    BulletNotFoundError,
    NoValidFactsError,
    UnknownPartError,
    recheck,
    replace_bullet_with_facts,
)

INTERN = "云栈物流科技 后端开发实习"
OTHER = "星桥数据 数据开发实习"


def fact(text, experience=INTERN, responsibility=Responsibility.OWN):
    return GroundingFact(
        fact_id=uuid4(),
        experience=experience,
        responsibility=responsibility,
        text=text,
        source=FactSource.RESUME,
        quote_verified=True,
        section=SectionKind.INTERNSHIP,
    )


API = fact("负责运单查询接口")
SPARK = fact("用 Spark 清洗日志", OTHER)
AVAILABLE = {f.fact_id: f for f in (API, SPARK)}
BULLET = bullet_of(uuid4(), "负责运单查询接口", [API.fact_id], CheckStatus.PASSED, [])
SKILL = bullet_of(  # 技能概述跨经历引用
    uuid4(), "运单查询、日志清洗", [API.fact_id, SPARK.fact_id], CheckStatus.PASSED, []
)
PROJECT = bullet_of(
    uuid4(), "搭建日志采集管道", [SPARK.fact_id], CheckStatus.PASSED, []
)
PREVIOUS = ResumeContent(
    header=["张三"],
    sections=[
        ResumeSection(
            title="实习经历",
            kind=SectionKind.INTERNSHIP,
            entries=[
                Entry(
                    entry_id="e1",
                    experience=INTERN,
                    heading=INTERN,
                    title=INTERN,
                    bullets=[BULLET],
                )
            ],
        ),
        ResumeSection(
            title="技能",
            kind=SectionKind.SKILL,
            entries=[
                Entry(
                    entry_id="e2",
                    experience=None,
                    heading="技能",
                    title="技能",
                    bullets=[SKILL],
                )
            ],
        ),
        ResumeSection(
            title="项目经历",
            kind=SectionKind.PROJECT,
            entries=[
                Entry(
                    entry_id="e3",
                    experience=OTHER,
                    heading=OTHER,
                    title=OTHER,
                    bullets=[PROJECT],
                )
            ],
        ),
    ],
)


def edit(text=None, **bullet_changes):
    """把第一条改写换成新文字（或别的字段），其他不动。"""
    content = PREVIOUS.model_copy(deep=True)
    bullet = content.sections[0].entries[0].bullets[0]
    if text is not None:
        bullet.text = text
    for name, value in bullet_changes.items():
        setattr(bullet, name, value)
    return content


def run(content):
    return recheck(PREVIOUS, content, AVAILABLE, {"云栈物流科技", "星桥数据"})


def first(content):
    return content.sections[0].entries[0].bullets[0]


def test_unchanged_bullet_keeps_its_check():
    result = first(run(edit()))
    assert result.check == BULLET.check and not result.edited


def test_edited_bullet_is_rechecked_by_program_layers():
    result = first(run(edit("主导运单查询接口")))
    assert result.edited and result.text == "主导运单查询接口"
    assert result.check.layers is CheckLayers.PROGRAM
    assert result.check.text == "主导运单查询接口"
    assert [i.kind for i in result.check.issues] == [IssueKind.ROLE]


def test_surrounding_spaces_do_not_count_as_an_edit():
    result = first(run(edit("  负责运单查询接口 ")))
    assert result.check == BULLET.check and not result.edited


def test_client_cannot_change_citations_or_origin():
    forged = edit(
        "负责运单查询接口，在星桥数据清洗日志",
        fact_ids=[API.fact_id, SPARK.fact_id],
        origin=CheckStatus.PASSED,
        edited=False,
    )
    result = first(run(forged))
    assert result.fact_ids == [API.fact_id]  # 上一版的引用
    # 按上一版的引用检查：「星桥数据」属于另一段经历，引用的事实里没有
    assert IssueKind.ORGANIZATION in {i.kind for i in result.check.issues}


def test_client_cannot_change_which_experience_an_entry_is():
    content = edit("负责运单查询接口")
    content.sections[0].entries[0].experience = OTHER
    assert run(content).sections[0].entries[0].experience == INTERN


def test_skills_bullets_may_cite_any_experience():
    content = PREVIOUS.model_copy(deep=True)
    content.sections[1].entries[0].bullets[0].text = "运单查询接口、清洗日志"
    result = run(content).sections[1].entries[0].bullets[0]
    assert result.check.layers is CheckLayers.PROGRAM and result.check.passed


def test_deleting_bullets_is_allowed():
    content = PREVIOUS.model_copy(deep=True)
    content.sections[0].entries[0].bullets.clear()
    result = run(content)
    assert result.sections[0].entries[0].bullets == []
    assert [b.bullet_id for b in result.sections[1].entries[0].bullets] == [
        SKILL.bullet_id
    ]


def move_bullet_to_the_project(content):
    """实习的条目挂到项目名下：ID 和文字都没变。"""
    content.sections[2].entries[0].bullets.append(
        content.sections[0].entries[0].bullets.pop()
    )


def duplicate_the_internship_entry(content):
    """同一段经历在技能栏里再出现一次，不带条目。"""
    content.sections[1].entries.append(
        content.sections[0].entries[0].model_copy(update={"bullets": []})
    )


def move_the_internship_entry_to_the_project_section(content):
    """整段经历连同条目挪进另一个栏目。"""
    content.sections[2].entries.insert(0, content.sections[0].entries.pop())


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # 文字没变就会沿用原来的通过结论：实习的成果会挂在项目名下
        (move_bullet_to_the_project, "条目换了所属经历"),
        (duplicate_the_internship_entry, "同一经历出现了不止一次"),
        (move_the_internship_entry_to_the_project_section, "经历换了栏目"),
    ],
)
def test_parts_cannot_leave_their_place(change, problem):
    """检查结论只对条目在上一版里的位置有效；编辑器只能原位改字，换了位置就拒绝。"""
    content = PREVIOUS.model_copy(deep=True)
    change(content)
    with pytest.raises(UnknownPartError) as error:
        run(content)
    assert any(problem in p for p in error.value.problems)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda c: setattr(c.sections[0].entries[0], "entry_id", "e9"), "不认识的经历"),
        (
            lambda c: setattr(
                c.sections[0].entries[0].bullets[0], "bullet_id", uuid4()
            ),
            "不认识的条目",
        ),
        (
            lambda c: (
                c.sections[1]
                .entries[0]
                .bullets.append(c.sections[0].entries[0].bullets[0])
            ),
            "不止一次",
        ),
    ],
)
def test_unknown_or_duplicate_parts_are_rejected(change, problem):
    content = PREVIOUS.model_copy(deep=True)
    change(content)
    with pytest.raises(UnknownPartError) as error:
        run(content)
    assert any(problem in p for p in error.value.problems)


def test_bolding_alone_is_not_an_edit():
    result = first(run(edit("负责**运单查询**接口")))
    assert result.text == "负责**运单查询**接口"
    assert result.check == BULLET.check and not result.edited
    assert result.check_current


def test_style_changes_are_kept():
    content = edit()
    content.style = content.style.model_copy(update={"size_pt": 9.0, "leading": 1.3})
    assert run(content).style == content.style


def test_replace_bullet_with_facts_assembles_valid_facts_and_resets_check():
    """不用模型写法、改用事实原文：按服务端保存的引用和有效事实重拼。"""
    twenty = fact("运单查询 P95 耗时降低 20%")
    unverified = GroundingFact(
        fact_id=uuid4(),
        experience=INTERN,
        responsibility=Responsibility.OWN,
        text="未校验的事实",
        source=FactSource.RESUME,
        quote_verified=False,
        section=SectionKind.INTERNSHIP,
    )
    bullet = bullet_of(
        uuid4(),
        "负责运单查询接口，P95 耗时降低 30%",
        [API.fact_id, unverified.fact_id, twenty.fact_id],
        CheckStatus.PASSED,
        [],
    )
    bullet = bullet.model_copy(update={"edited": True})
    content = PREVIOUS.model_copy(deep=True)
    content.sections[0].entries[0].bullets = [bullet]

    updated = replace_bullet_with_facts(
        content,
        bullet.bullet_id,
        {**AVAILABLE, twenty.fact_id: twenty, unverified.fact_id: unverified},
    )
    replaced = updated.sections[0].entries[0].bullets[0]
    assert replaced.text == "负责运单查询接口；运单查询 P95 耗时降低 20%"
    assert replaced.fact_ids == [API.fact_id, twenty.fact_id]
    assert replaced.origin is CheckStatus.FALLBACK
    assert not replaced.edited
    assert replaced.check.text == replaced.text
    assert replaced.check.passed

    # 技能概述区允许跨经历引用
    skill_updated = replace_bullet_with_facts(updated, SKILL.bullet_id, AVAILABLE)
    assert (
        skill_updated.sections[1].entries[0].bullets[0].text
        == "负责运单查询接口；用 Spark 清洗日志"
    )


def test_replace_bullet_with_facts_rejects_missing_bullet_or_no_valid_facts():
    with pytest.raises(BulletNotFoundError):
        replace_bullet_with_facts(PREVIOUS, uuid4(), AVAILABLE)

    # 引用的事实都不存在或失效
    with pytest.raises(NoValidFactsError):
        replace_bullet_with_facts(PREVIOUS, BULLET.bullet_id, {})
