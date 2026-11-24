"""依据检查第一、二层：六类编造各有多个用例，正常改写也要足够多，防止误拦。

事实库是合成的：一段实习、一个项目、技能概述和教育经历。
"""

from pathlib import Path
from uuid import uuid4

import pytest

from jobpilot.db.models import FactSource, Responsibility
from jobpilot.services.grounding import (
    BulletSection,
    GroundingFact,
    IssueKind,
    check_grounding,
    organizations_of,
    should_run_semantic,
    technologies_in,
)
from jobpilot.services.grounding_rules import (
    DEFAULT_RULES,
    GroundingRulesError,
    load_grounding_rules,
)
from jobpilot.services.keywords import DEFAULT_TABLE, load_keyword_table

INTERN = "云栈物流科技 后端开发实习"
PROJECT = "校园二手书交易小程序"
SKILLS = "专业技能"
SCHOOL = "澄湖大学 计算机科学与技术 本科"

OWN, PART, LEAD, NA = (
    Responsibility.OWN,
    Responsibility.PARTICIPATE,
    Responsibility.LEAD,
    Responsibility.NA,
)


def _fact(name: str, experience: str, responsibility: Responsibility, text: str):
    return name, GroundingFact(
        fact_id=uuid4(),
        experience=experience,
        responsibility=responsibility,
        text=text,
        source=FactSource.RESUME,
        quote_verified=True,
    )


FACTS = dict(
    [
        _fact("a1", INTERN, OWN, "负责运单查询接口"),
        _fact("a2", INTERN, OWN, "使用 Go 和 Redis 缓存热点运单"),
        _fact("a3", INTERN, PART, "运单查询 P95 耗时由 400 毫秒降至 260 毫秒"),
        _fact("a4", INTERN, PART, "补充接口超时的告警规则"),
        _fact("b1", PROJECT, LEAD, "牵头四人团队完成小程序开发与上线"),
        _fact("b2", PROJECT, OWN, "用 Flask 实现订单接口"),
        _fact("b3", PROJECT, PART, "搜索响应时间由 800 毫秒降至 300 毫秒"),
        _fact("b4", PROJECT, PART, "编写 15 条接口测试"),
        _fact("c1", SKILLS, NA, "熟悉 Python 和 MySQL"),
        _fact("c2", SKILLS, NA, "了解 Kafka"),
        _fact("d1", SCHOOL, NA, "主修操作系统、编译原理"),
    ]
)
AVAILABLE = {fact.fact_id: fact for fact in FACTS.values()}
ORGS = organizations_of({fact.experience for fact in FACTS.values()})


def check(text: str, *names: str, section=BulletSection.EXPERIENCE, experience=None):
    return check_grounding(
        text,
        [FACTS[name].fact_id for name in names],
        AVAILABLE,
        section=section,
        experience=experience,
        known_organizations=ORGS,
    )


def kinds(result) -> list[IssueKind]:
    return [issue.kind for issue in result.issues]


# ---------- 正常改写：一条都不能拦 ----------

NORMAL = [
    ("负责运单查询接口，使用 Go 和 Redis 缓存热点运单", ["a1", "a2"]),
    ("运单查询 P95 耗时从 400ms 降至 260ms", ["a3"]),  # 毫秒与 ms 等价
    ("运单查询 P95 耗时由４００毫秒降至２６０毫秒", ["a3"]),  # 全角数字
    ("用 Golang 与 Redis 缓存热点运单，降低查询压力", ["a2"]),  # 同义词
    ("参与补充接口超时告警规则", ["a4"]),  # 参与级配参与级
    ("牵头团队完成二手书小程序的开发与上线", ["b1"]),  # 主导级配主导级
    ("负责用 Flask 实现订单接口，搜索响应由 800 毫秒降至 300 毫秒", ["b2", "b3"]),
    ("编写 15 个接口测试", ["b4"]),  # 计数单位之间可以互换
    ("熟悉 Redis，用它缓存热点运单", ["a2"]),  # 没写程度时最多到「熟悉」
    ("在云栈物流科技负责运单查询接口", ["a1"]),  # 本段经历的组织名
    ("负责运单查询接口；参与补充超时告警规则", ["a1", "a4"]),  # 最高职责为负责
    ("缓存热点运单，查询 P95 从 400 毫秒降到 260 毫秒", ["a2", "a3"]),
    ("基于 Flask 开发订单接口并编写 15 条接口测试", ["b2", "b4"]),
    ("使用 Go、Redis 缓存运单热点数据", ["a2"]),
]


@pytest.mark.parametrize(("text", "names"), NORMAL)
def test_normal_rewrites_pass(text: str, names: list[str]) -> None:
    result = check(text, *names)
    assert result.passed, [issue.message for issue in result.issues]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("熟悉 Python、MySQL，了解 Kafka", ["c1", "c2"]),
        ("熟悉 Python，使用过 Go、Redis 与 Flask", ["c1", "a2", "b2"]),
    ],
)
def test_skills_section_may_cite_several_experiences(text, names) -> None:
    assert check(text, *names, section=BulletSection.SKILLS).passed


# ---------- 凭空添加：技术名 ----------


@pytest.mark.parametrize(
    ("text", "names", "fragment"),
    [
        ("使用 Docker 部署运单查询接口", ["a1"], "Docker"),
        (
            "用 Go、Redis 和 Kafka 缓存热点运单",
            ["a2"],
            "Kafka",
        ),  # Kafka 在别的事实里，没被引用
        ("基于 LLM 优化运单查询接口", ["a1"], "LLM"),
        ("用大模型优化运单查询接口", ["a1"], "大模型"),
        ("用 K8s 编排运单查询服务", ["a1"], "K8s"),
        ("用 Flask 和 PostgreSQL 实现订单接口", ["b2"], "PostgreSQL"),
    ],
)
def test_added_technology_is_flagged(text, names, fragment) -> None:
    result = check(text, *names)
    assert kinds(result) == [IssueKind.TECHNOLOGY]
    assert result.issues[0].fragment == fragment


def test_technology_matching_respects_word_boundaries() -> None:
    table = load_keyword_table(DEFAULT_TABLE)
    assert set(technologies_in("JavaScript 与 Google 地图", table)) == {"JavaScript"}
    assert set(technologies_in("多模态大模型", table)) == {"多模态大模型"}
    assert set(technologies_in("C++ 和 Go语言", table)) == {"C++", "Go"}


# ---------- 数字编造或放大 ----------


@pytest.mark.parametrize(
    ("text", "names", "fragment"),
    [
        ("P95 耗时由 400 毫秒降至 200 毫秒", ["a3"], "200 毫秒"),
        ("运单查询耗时降低 35%", ["a3"], "35%"),  # 算出来的比例也拦，宁可误拦
        ("运单查询耗时降低 400%", ["a3"], "400%"),  # 400 有，但单位是毫秒不是百分比
        ("编写 50 条接口测试", ["b4"], "50"),
        ("支撑日均 10万 单运单查询", ["a1"], "10万"),
        ("搜索响应时间降至 30 毫秒", ["b3"], "30 毫秒"),
        ("运单查询耗时降至 260 秒", ["a3"], "260 秒"),  # 秒和毫秒不能互换
    ],
)
def test_changed_or_added_number_is_flagged(text, names, fragment) -> None:
    result = check(text, *names)
    assert kinds(result) == [IssueKind.NUMBER]
    assert result.issues[0].fragment == fragment


def test_number_formats_are_normalized() -> None:
    facts = {
        fact.fact_id: fact
        for _, fact in [
            _fact("x", INTERN, PART, "日均处理 10,000 单，成功率 99.0%"),
        ]
    }
    result = check_grounding(
        "日均处理 10000 单，成功率 99%",
        list(facts),
        facts,
        section=BulletSection.EXPERIENCE,
    )
    assert result.passed


def test_digits_inside_names_are_not_numbers() -> None:
    # P95 的 95 属于名字，不当作数字；「5 sessions」里的 s 不是秒。
    assert check("优化运单查询 P95 耗时", "a3").passed
    _, fact = _fact("y", INTERN, PART, "处理 5 个会话")
    result = check_grounding(
        "处理 5 sessions",
        [fact.fact_id],
        {fact.fact_id: fact},
        section=BulletSection.EXPERIENCE,
    )
    assert result.passed


# ---------- 角色升级 ----------


@pytest.mark.parametrize(
    ("text", "names", "word"),
    [
        ("主导运单查询接口开发", ["a1"], "主导"),  # 负责 → 主导
        ("负责补充接口超时的告警规则", ["a4"], "负责"),  # 参与 → 负责
        ("独立完成接口超时告警规则", ["a4"], "独立完成"),
        ("牵头补充接口超时告警规则", ["a4"], "牵头"),
        ("作为负责人搭建 Flask 订单接口", ["b2"], "负责人"),  # 「负责人」是主导级
        ("带领团队优化运单查询 P95 耗时", ["a3"], "带领"),
    ],
)
def test_role_upgrade_is_flagged(text, names, word) -> None:
    result = check(text, *names)
    assert kinds(result) == [IssueKind.ROLE]
    assert result.issues[0].fragment == word


@pytest.mark.parametrize("word", ["负责", "参与"])
def test_no_role_words_when_all_cited_facts_are_na(word) -> None:
    result = check(f"{word}操作系统、编译原理课程学习", "d1")
    assert kinds(result) == [IssueKind.ROLE]


# ---------- 程度升级 ----------


@pytest.mark.parametrize(
    ("text", "names", "word"),
    [
        ("精通 Python 和 MySQL", ["c1"], "精通"),  # 熟悉 → 精通
        ("熟练掌握 Kafka", ["c2"], "熟练掌握"),  # 了解 → 熟练
        ("掌握 Kafka 的使用", ["c2"], "掌握"),
        ("熟悉 Kafka", ["c2"], "熟悉"),  # 了解 → 熟悉也是升级
        ("精通 Redis，用它缓存热点运单", ["a2"], "精通"),  # 没写程度，上限是熟悉
        ("擅长用 Flask 实现订单接口", ["b2"], "擅长"),
    ],
)
def test_degree_upgrade_is_flagged(text, names, word) -> None:
    result = check(text, *names, section=BulletSection.SKILLS)
    assert kinds(result) == [IssueKind.DEGREE]
    assert result.issues[0].fragment == word


def test_unstated_degree_caps_at_familiar() -> None:
    assert check("熟悉操作系统与编译原理", "d1").passed
    assert kinds(check("掌握操作系统与编译原理", "d1")) == [IssueKind.DEGREE]


# ---------- 张冠李戴 ----------


def test_experience_bullet_citing_two_experiences_is_flagged() -> None:
    result = check("使用 Flask 和 Redis 实现订单接口", "b2", "a2")
    assert kinds(result) == [IssueKind.CROSS_EXPERIENCE]


def test_bullet_under_wrong_experience_is_flagged() -> None:
    result = check("用 Flask 实现订单接口", "b2", experience=INTERN)
    assert kinds(result) == [IssueKind.CROSS_EXPERIENCE]
    assert PROJECT in result.issues[0].message
    assert check("用 Flask 实现订单接口", "b2", experience=PROJECT).passed


@pytest.mark.parametrize(
    ("text", "names", "org"),
    [
        ("在澄湖大学使用 Go 缓存热点运单", ["a2"], "澄湖大学"),
        ("为校园二手书交易小程序缓存热点运单", ["a2"], PROJECT),
        ("在云栈物流科技用 Flask 实现订单接口", ["b2"], "云栈物流科技"),
    ],
)
def test_organization_from_another_experience_is_flagged(text, names, org) -> None:
    result = check(text, *names)
    assert kinds(result) == [IssueKind.ORGANIZATION]
    assert result.issues[0].fragment == org


def test_section_names_are_not_organizations() -> None:
    names = ["技能", "教育经历", "项目经历 补充", "专业技能", INTERN]
    assert organizations_of(names) == {"云栈物流科技"}


def test_organizations_are_first_words_of_experience_names() -> None:
    assert organizations_of([INTERN, "A 项目", PROJECT]) == {"云栈物流科技", PROJECT}


# ---------- 第一层：结构 ----------


def test_bullet_without_citation_is_flagged() -> None:
    result = check("负责运单查询接口")
    assert kinds(result) == [IssueKind.MISSING_CITATION]


def test_unavailable_fact_is_flagged_and_others_still_checked() -> None:
    # 别人的事实、别的版本的事实，调用方都不会放进 available，这里统一表现为不可用。
    result = check_grounding(
        "主导运单查询接口",
        [uuid4(), FACTS["a1"].fact_id],
        AVAILABLE,
        section=BulletSection.EXPERIENCE,
    )
    assert kinds(result) == [IssueKind.UNAVAILABLE_FACT, IssueKind.ROLE]
    assert "不在本次使用的事实库里" in result.issues[0].message


def test_only_unavailable_facts_skip_content_checks() -> None:
    result = check_grounding(
        "主导用 Docker 部署", [uuid4()], AVAILABLE, section=BulletSection.EXPERIENCE
    )
    assert kinds(result) == [IssueKind.UNAVAILABLE_FACT]


def test_unverified_resume_fact_is_flagged_but_user_fact_is_not() -> None:
    unverified = GroundingFact(
        uuid4(), INTERN, OWN, "负责运单查询接口", FactSource.RESUME, False
    )
    added = GroundingFact(
        uuid4(), INTERN, OWN, "负责运单查询接口", FactSource.USER, False
    )
    available = {unverified.fact_id: unverified, added.fact_id: added}
    bad = check_grounding(
        "负责运单查询接口",
        [unverified.fact_id],
        available,
        section=BulletSection.EXPERIENCE,
    )
    good = check_grounding(
        "负责运单查询接口",
        [added.fact_id],
        available,
        section=BulletSection.EXPERIENCE,
    )
    assert kinds(bad) == [IssueKind.UNVERIFIED_QUOTE]
    assert good.passed


def test_duplicate_citations_are_counted_once() -> None:
    assert check("负责运单查询接口", "a1", "a1").passed


def test_several_problems_are_all_reported() -> None:
    result = check("主导用 Docker 部署运单服务，耗时降低 35%", "a1", "a3")
    assert set(kinds(result)) == {
        IssueKind.NUMBER,
        IssueKind.TECHNOLOGY,
        IssueKind.ROLE,
    }


# ---------- 已知局限：这些情况第二层查不出来，交给第三层 ----------
# 断言的是现在的行为。哪天补上了，这些用例会失败，提醒更新 README 的局限说明。


def test_known_limit_unknown_technology_is_missed() -> None:
    assert check("用 Pulsar 缓存热点运单", "a2").passed  # 同义词表里没有 Pulsar


def test_known_limit_unknown_organization_is_missed() -> None:
    assert check("在清华大学负责运单查询接口", "a1").passed


def test_known_limit_chinese_numerals_are_not_checked() -> None:
    assert check("牵头五人团队完成小程序开发与上线", "b1").passed


def test_known_limit_derived_numbers_are_blocked() -> None:
    # 事实写「一周内」，改写换算成「7 天内」也会被拦：换算出来的数字一律不认，宁可误拦。
    _, fact = _fact("w", INTERN, PART, "上线后一周内跟进告警")
    result = check_grounding(
        "上线后 7 天内跟进告警",
        [fact.fact_id],
        {fact.fact_id: fact},
        section=BulletSection.EXPERIENCE,
    )
    assert kinds(result) == [IssueKind.NUMBER]


def test_known_limit_negation_is_ignored() -> None:
    assert kinds(check("不负责告警规则，只参与补充", "a4")) == [IssueKind.ROLE]


# ---------- 词表 ----------


def test_project_rules_load() -> None:
    rules = load_grounding_rules(DEFAULT_RULES)
    assert rules.role_levels["负责人"] is Responsibility.LEAD
    assert rules.degree_names[rules.unstated_degree_max] == "熟悉"


def test_duplicate_word_in_rules_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "role_levels:\n  lead: [主导]\n  own: [主导]\n"
        "degree_levels:\n  - {name: 了解, words: [了解]}\n"
        "unstated_degree_max: 了解\n",
        encoding="utf-8",
    )
    with pytest.raises(GroundingRulesError, match="重复"):
        load_grounding_rules(path)


def test_unknown_unstated_level_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "role_levels:\n  lead: [主导]\n"
        "degree_levels:\n  - {name: 了解, words: [了解]}\n"
        "unstated_degree_max: 精通\n",
        encoding="utf-8",
    )
    with pytest.raises(GroundingRulesError, match="unstated_degree_max"):
        load_grounding_rules(path)


# ---------- 要不要调第三层 ----------


def test_semantic_check_runs_when_first_two_layers_pass() -> None:
    assert should_run_semantic([], revisions_left=2)
    assert should_run_semantic([], revisions_left=0)


def test_semantic_check_skipped_when_structure_fails() -> None:
    issues = check("负责运单查询接口").issues  # 没有引用
    for left in (0, 1, 2):
        assert not should_run_semantic(issues, revisions_left=left)


def test_content_failure_calls_semantic_only_before_last_revision() -> None:
    issues = check("主导运单查询接口开发", "a1").issues
    assert kinds_of(issues) == [IssueKind.ROLE]
    assert not should_run_semantic(issues, revisions_left=2)  # 第一次检查：先修
    assert should_run_semantic(issues, revisions_left=1)  # 最后一次修订前：问题给全
    assert not should_run_semantic(issues, revisions_left=0)  # 反正要回退


def kinds_of(issues) -> list[IssueKind]:
    return [issue.kind for issue in issues]


# ---------- 中文数字、年份、职责不适用时的角色词 ----------


def _single(text: str, responsibility=PART, experience=INTERN):
    _, fact = _fact("z", experience, responsibility, text)
    return fact


def _check_one(sentence: str, fact: GroundingFact):
    return check_grounding(
        sentence, [fact.fact_id], {fact.fact_id: fact}, section=BulletSection.EXPERIENCE
    )


@pytest.mark.parametrize(
    ("fact_text", "sentence"),
    [
        ("牵头四人团队完成小程序开发与上线", "牵头 4 人团队完成小程序上线"),
        ("兼容两种表头", "兼容 2 种表头"),
        (
            "前十位至少出现一个相关商品的查询从 49 条增至 56 条",
            "Top-10 命中查询从 49 条增至 56 条",
        ),
        ("每百张图误报数从 6 个增加到 10 个", "每 100 张图误报从 6 个增至 10 个"),
        ("通过大学英语六级", "通过 CET-6"),
        ("一百二十条历史工单", "复核 120 条历史工单"),
        ("一百零五条记录", "整理 105 条记录"),
        ("连续十二周值班", "连续值班 12 周"),
        ("雅思 7.0（2025）", "2025 年考取雅思 7.0"),
    ],
)
def test_chinese_numerals_and_years_in_facts_support_digits(
    fact_text, sentence
) -> None:
    fact = _single(fact_text, responsibility=LEAD)
    result = _check_one(sentence, fact)
    assert result.passed, [issue.message for issue in result.issues]


@pytest.mark.parametrize(
    ("fact_text", "sentence", "fragment"),
    [
        ("兼容两种表头", "兼容 3 种表头", "3"),  # 计数量词不进片段
        ("一周内跟进", "两周内跟进，共 2 周", "2 周"),  # 一周不能写成 2 周
        (
            "一起梳理回归用例",
            "梳理 1 批回归用例",
            "1",
        ),  # 「一起」后面不是量词，不算数字 1
        ("雅思 7.0（2025）", "2024 年考取雅思 7.0", "2024 年"),
    ],
)
def test_chinese_numerals_do_not_cover_other_numbers(
    fact_text, sentence, fragment
) -> None:
    result = _check_one(sentence, _single(fact_text))
    assert kinds(result) == [IssueKind.NUMBER]
    assert result.issues[0].fragment == fragment


def test_chinese_numerals_in_rewrite_are_left_to_layer_three() -> None:
    # 改写里的中文数字不检查：「一次性」不当成 1，「五人」写错了也查不出，交给第三层。
    assert _check_one("一次性完成接口开发", _single("完成接口开发")).passed


@pytest.mark.parametrize(
    ("fact_text", "sentence"),
    [
        ("负责晚班签到和物资登记", "负责学院迎新晚班签到与物资登记"),
        ("协助登记借用设备", "协助登记借用设备并汇总记录"),
        ("负责晚班签到", "协助晚班签到"),  # 低于原文等级也可以
    ],
)
def test_na_fact_allows_role_words_its_text_states(fact_text, sentence) -> None:
    assert _check_one(sentence, _single(fact_text, responsibility=NA)).passed


@pytest.mark.parametrize(
    ("fact_text", "sentence", "word"),
    [
        ("协助登记借用设备", "负责登记借用设备", "负责"),  # 高于原文
        (
            "主要做需求拆解和客户验收",
            "主导需求拆解和客户验收",
            "主导",
        ),  # 原文没有角色词
    ],
)
def test_na_fact_still_blocks_role_words_beyond_its_text(
    fact_text, sentence, word
) -> None:
    result = _check_one(sentence, _single(fact_text, responsibility=NA))
    assert kinds(result) == [IssueKind.ROLE]
    assert result.issues[0].fragment == word
