"""完整简历组装：标题行拆分、栏目名识别、按底稿切段、换入改写条目、顺序与补入。纯函数。"""

from uuid import uuid4

import pytest

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import CheckStatus, SectionKind
from jobpilot.services.grounding import GroundingIssue, IssueKind
from jobpilot.services.resume_content import (
    SKILLS_TITLE,
    BaseSection,
    BlockBullets,
    CheckLayers,
    assemble,
    bullet_of,
    is_section_title,
    split_heading,
)

RAW = (REPO_ROOT / "evals/datasets/resumes/resume-013.txt").read_text(encoding="utf-8")
WORK = "砚桥商服软件  /  Java 开发工程师  /  2024.07 至今"
MONTHLY = "门店月结清单（2025.08–2026.10）"
VOUCHER = "外部凭证同步（2024.11–2025.06）"
INTERN = "同川数服  /  Java 实习生  /  2023.07–2023.10"
SCHOOL = "宁沅工学院 | 计算机科学与技术 | 本科 | 2020.09–2024.06"
STACK = "Java 17、Spring Boot、MyBatis、MySQL、Redis。"
# (经历名, 段落类型, 标题行)：与事实拆分记下的 sections_json 一致，每段到下一个标题为止
HEADINGS = [
    ("砚桥商服软件 Java 开发工程师", SectionKind.INTERNSHIP, WORK),
    ("门店月结清单", SectionKind.PROJECT, MONTHLY),
    ("外部凭证同步", SectionKind.PROJECT, VOUCHER),
    ("同川数服 Java 实习", SectionKind.INTERNSHIP, INTERN),
    ("宁沅工学院", SectionKind.EDUCATION, SCHOOL),
    ("技术栈", SectionKind.SKILL, STACK),
]


def base_sections(raw: str = RAW, headings=HEADINGS) -> list[BaseSection]:
    starts = [raw.index(line) for *_, line in headings]
    ends = [*starts[1:], len(raw)]
    return [
        BaseSection(name=name, section=kind, start=start, end=end)
        for (name, kind, _), start, end in zip(headings, starts, ends, strict=True)
    ]


def bullets(*texts: str, status=CheckStatus.PASSED):
    return [bullet_of(uuid4(), text, [uuid4()], status, []) for text in texts]


def block(experience, section, *texts):
    return BlockBullets(experience=experience, section=section, bullets=bullets(*texts))


# ---------- 标题行与栏目名 ----------


@pytest.mark.parametrize(
    ("line", "title", "time", "rest"),
    [
        (WORK, "砚桥商服软件  /  Java 开发工程师", "2024.07 至今", ""),
        (MONTHLY, "门店月结清单", "2025.08–2026.10", ""),
        (SCHOOL, "宁沅工学院 | 计算机科学与技术 | 本科", "2020.09–2024.06", ""),
        (
            "湘桐师范学院，信息与计算科学本科，2023.09–2027.06（预计）",
            "湘桐师范学院，信息与计算科学本科",
            "2023.09–2027.06（预计）",
            "",
        ),
        (
            "学校东门便利店，周末店员（2024.09–2025.06）。负责补货、交接和核对小票",
            "学校东门便利店，周末店员",
            "2024.09–2025.06",
            "负责补货、交接和核对小票",
        ),
        ("晚自习教室管理页面 / 课程小组", "晚自习教室管理页面 / 课程小组", None, ""),
        # 名称里也有年份时，时间取最后一处
        (
            "澄湖大学 2021 级 | 2021.09–2025.06",
            "澄湖大学 2021 级",
            "2021.09–2025.06",
            "",
        ),
    ],
)
def test_heading_splits_into_title_time_and_trailing_text(line, title, time, rest):
    assert split_heading(line) == (title, time, rest)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("工作经历", True),
        ("技术与工具", True),
        ("学历与技能", True),
        ("兼职", True),
        ("负责月结清单生成服务。", False),  # 句子
        ("负责项目。", False),  # 短，也有栏目词，但带句号
        ("Java 17、Spring Boot", False),  # 没有栏目词
        ("实习期间参与了三个项目的上线", False),  # 太长
    ],
)
def test_section_title_detection(line, expected):
    assert is_section_title(line) is expected


# ---------- 组装 ----------


def full_plan():
    return [
        block(
            "砚桥商服软件 Java 开发工程师", SectionKind.INTERNSHIP, "负责月结清单接口"
        ),
        block("门店月结清单", SectionKind.PROJECT, "负责月结清单生成服务"),
        block("同川数服 Java 实习", SectionKind.INTERNSHIP, "维护巡检单导出"),
        block(None, SectionKind.SKILL, "Java、Spring Boot、MySQL、Redis"),
    ]


def test_header_titles_and_unrewritten_parts_come_verbatim_from_the_base():
    content = assemble(RAW, base_sections(), full_plan())

    assert content.header == [
        "唐思齐",
        "南京 | siqi.tang@example.invalid | 求职意向：Java 后端开发 | "
        "到岗需交接，预计一个月",
    ]
    assert [s.title for s in content.sections] == [
        "工作经历",
        None,  # 项目段，底稿里没有单独的栏目名
        None,
        "教育经历",
        "技术栈",
    ]
    # 改写以外的每一段文字都出自底稿
    texts = [*content.header]
    for section in content.sections:
        texts += [section.title or ""]
        for entry in section.entries:
            texts += [entry.heading, *entry.lines]
    assert all(text in RAW for text in texts)


def test_rewritten_entries_use_bullets_and_others_keep_their_lines():
    content = assemble(RAW, base_sections(), full_plan())
    entries = {e.experience: e for s in content.sections for e in s.entries}

    work = entries["砚桥商服软件 Java 开发工程师"]
    assert (work.title, work.time) == (
        "砚桥商服软件  /  Java 开发工程师",
        "2024.07 至今",
    )
    assert [b.text for b in work.bullets] == ["负责月结清单接口"] and work.lines == []
    # 没改写的项目原样保留正文
    voucher = entries["外部凭证同步"]
    assert voucher.bullets == [] and voucher.lines[0].startswith("- 参与对接财务方")
    # 教育不改写
    school = entries["宁沅工学院"]
    assert (school.title, school.time, school.lines) == (
        "宁沅工学院 | 计算机科学与技术 | 本科",
        "2020.09–2024.06",
        [],
    )
    # 技能概述替换技能栏的正文（标题行本身是技能栏的第一句）
    skills = entries["技术栈"]
    assert [b.text for b in skills.bullets] == ["Java、Spring Boot、MySQL、Redis"]


def test_rewritten_entries_follow_plan_order_within_their_section():
    """计划把第二段实习排在前面：两段在同一栏时互换，栏目本身的位置不变。"""
    headings = [HEADINGS[0], HEADINGS[3], HEADINGS[4], HEADINGS[5]]  # 两段实习相邻
    plan = [
        block("同川数服 Java 实习", SectionKind.INTERNSHIP, "维护巡检单导出"),
        block("砚桥商服软件 Java 开发工程师", SectionKind.INTERNSHIP, "负责接口"),
    ]
    content = assemble(RAW, base_sections(headings=headings), plan)
    work = content.sections[0]
    assert [e.experience for e in work.entries] == [
        "同川数服 Java 实习",
        "砚桥商服软件 Java 开发工程师",
    ]


def test_experience_missing_from_base_is_added_to_its_section():
    """事实来自另一份简历的经历，补在同类栏目的最后，标题用经历名。"""
    plan = [
        *full_plan(),
        block("星桥数据 数据开发实习", SectionKind.INTERNSHIP, "清洗日志"),
    ]
    content = assemble(RAW, base_sections(), plan)
    last_internship = [s for s in content.sections if s.kind is SectionKind.INTERNSHIP][
        -1
    ]
    assert last_internship.entries[-1].heading == "星桥数据 数据开发实习"
    assert [b.text for b in last_internship.entries[-1].bullets] == ["清洗日志"]


def test_skills_without_a_base_skill_section_get_their_own_section():
    content = assemble(RAW, base_sections(headings=HEADINGS[:5]), full_plan())
    assert content.sections[-1].title == SKILLS_TITLE
    assert [b.text for b in content.sections[-1].entries[0].bullets] == [
        "Java、Spring Boot、MySQL、Redis"
    ]


def test_base_without_located_sections_keeps_everything_as_header():
    content = assemble("张三\n电话 123\n", [], [])
    assert (content.header, content.sections) == (["张三", "电话 123"], [])


# ---------- 最后一段的末行 ----------

SCHOOL_LINE = "澄湖大学 | 本科 | 2022.09–2026.06"
CLUB_LINE = "摄影社 | 2023.09–2024.06"


def test_last_section_keeps_its_final_line():
    """最后一段是教育，末行单独一行专业名：短、含「教育」、没有标点，不能当成栏目名拿走。"""
    headings = [
        ("同川数服 Java 实习", SectionKind.INTERNSHIP, INTERN),
        ("澄湖大学", SectionKind.EDUCATION, SCHOOL_LINE),
    ]
    raw = (
        f"周芷\n\n实习经历\n{INTERN}\n维护巡检单导出\n"
        f"\n教育经历\n{SCHOOL_LINE}\n教育技术学\n"
    )

    content = assemble(raw, base_sections(raw, headings), [])

    [intern, school] = content.sections
    assert (intern.title, intern.entries[0].lines) == (
        "实习经历",
        ["维护巡检单导出"],
    )
    assert (school.title, school.entries[0].lines) == ("教育经历", ["教育技术学"])


def test_draft_without_located_sections_keeps_a_title_like_final_line():
    """整份没分出经历时全是页头，末行即使是「兼职」这样的词也留着。"""
    content = assemble("张三\n电话 123\n兼职\n", [], [])
    assert (content.header, content.sections) == (["张三", "电话 123", "兼职"], [])


def test_section_title_before_another_section_is_still_taken():
    """真的还有下一段时，末尾的栏目名照常拿给下一段；最后一段的末行不拿。"""
    headings = [
        ("同川数服 Java 实习", SectionKind.INTERNSHIP, INTERN),
        ("摄影社", SectionKind.OTHER, CLUB_LINE),
        ("澄湖大学", SectionKind.EDUCATION, SCHOOL_LINE),
    ]
    raw = (
        f"周芷\n实习经历\n{INTERN}\n维护巡检单导出\n校园经历\n{CLUB_LINE}\n"
        f"组织迎新\n教育经历\n{SCHOOL_LINE}\n教育技术学\n"
    )

    content = assemble(raw, base_sections(raw, headings), [])

    assert [(s.title, [e.lines for e in s.entries]) for s in content.sections] == [
        ("实习经历", [["维护巡检单导出"]]),
        ("校园经历", [["组织迎新"]]),
        ("教育经历", [["教育技术学"]]),
    ]


# ---------- 检查结论绑定文字 ----------


def test_fallback_check_describes_the_fact_text_not_the_discarded_sentence():
    issue = GroundingIssue(
        kind=IssueKind.ROLE, message="「主导」高于职责", fragment="主导"
    )
    fallback = bullet_of(uuid4(), "负责接口", [uuid4()], CheckStatus.FALLBACK, [issue])
    pending = bullet_of(uuid4(), "主导接口", [uuid4()], CheckStatus.PENDING, [issue])

    assert fallback.check.passed and fallback.check.layers is CheckLayers.PROGRAM
    assert not pending.check.passed and pending.check.layers is CheckLayers.FULL


def test_check_is_stale_once_the_text_changes():
    [bullet] = bullets("负责月结清单接口")
    assert bullet.check_current
    edited = bullet.model_copy(update={"text": "主导月结清单接口"})
    assert not edited.check_current
