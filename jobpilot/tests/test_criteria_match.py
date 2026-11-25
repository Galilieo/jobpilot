"""求职条件与教育的纯函数程序比对测试（不访问数据库、不调模型）。"""

from uuid import uuid4

from jobpilot.db.models import CriterionField, DegreeLevel, EducationStatus
from jobpilot.services.criteria_match import (
    CriterionCandidate,
    EducationCandidate,
    evaluate_requirement,
    sync_main_gaps,
)
from jobpilot.services.jobs import BasicFields, Requirement, RequirementType


def _days(n: int) -> CriterionCandidate:
    return CriterionCandidate(
        ref_id="criteria.onsite_days_per_week",
        field=CriterionField.ONSITE_DAYS_PER_WEEK,
        int_value=n,
        text_value=None,
        text=f"每周可到岗 {n} 天",
    )


def _months(n: int) -> CriterionCandidate:
    return CriterionCandidate(
        ref_id="criteria.internship_months",
        field=CriterionField.INTERNSHIP_MONTHS,
        int_value=n,
        text_value=None,
        text=f"可实习 {n} 个月",
    )


def _edu(
    school: str, degree: DegreeLevel, status: EducationStatus
) -> EducationCandidate:
    eid = uuid4()
    return EducationCandidate(
        ref_id=f"education.{eid}",
        education_id=eid,
        school=school,
        major="计算机",
        degree=degree,
        status=status,
        start_text="2023.09",
        end_text="2027.06",
        text=f"{school} · 计算机 · {degree.value}",
    )


def test_onsite_days_covered_below_and_missing() -> None:
    req = Requirement(type=RequirementType.HARD, text="每周至少到岗 4 天")

    covered, note = evaluate_requirement(req, [_days(5)], [])
    assert note is None and covered is not None
    assert covered.verdict == "covered"
    assert covered.criteria_refs == ("criteria.onsite_days_per_week",)

    below, note = evaluate_requirement(req, [_days(3)], [])
    assert note is None and below is not None
    assert below.verdict == "partial"
    assert "低于岗位要求" in below.reason

    missing, note = evaluate_requirement(req, [], [])
    assert note is None and missing is not None
    assert missing.verdict == "missing"
    assert "求职条件" in missing.suggestion
    assert "写进简历" not in missing.suggestion or "不要" in missing.suggestion


def test_internship_months_range_preserves_bounds() -> None:
    req = Requirement(type=RequirementType.HARD, text="连续实习 3～6 个月")

    ok, _ = evaluate_requirement(req, [_months(4)], [])
    assert ok is not None and ok.verdict == "covered"

    too_short, _ = evaluate_requirement(req, [_months(2)], [])
    assert too_short is not None and too_short.verdict == "partial"
    assert "低于岗位要求" in too_short.reason

    too_long, _ = evaluate_requirement(req, [_months(8)], [])
    assert too_long is not None and too_long.verdict == "partial"


def test_compound_requirement_reads_as_sentences() -> None:
    """多个子句的理由和建议逐句接起来，不出现「。；」或括号里带句号。"""
    req = Requirement(
        type=RequirementType.HARD, text="每周至少到岗 4 天，可实习至少 3 个月"
    )
    both, _ = evaluate_requirement(req, [_days(4), _months(4)], [])
    assert both is not None and both.verdict == "covered"
    assert both.reason == (
        "已确认每周可到岗天数为 4 天/周，满足岗位要求。"
        "已确认可实习月数为 4 个月，满足岗位要求。"
    )

    edu_req = Requirement(type=RequirementType.HARD, text="本科及以上，硕士优先")
    bachelor = _edu("甲大学", DegreeLevel.BACHELOR, EducationStatus.ENROLLED)
    with_note, _ = evaluate_requirement(edu_req, [], [bachelor])
    assert with_note is not None
    for text in (with_note.reason, with_note.suggestion):
        assert "。；" not in text and "。）" not in text


def test_bachelor_required_master_preferred() -> None:
    """「本科及以上，硕士优先」：本科用户硬性满足，同时写明优先条件未满足。"""
    req = Requirement(type=RequirementType.HARD, text="本科及以上，硕士优先")

    bachelor = _edu("东澜理工", DegreeLevel.BACHELOR, EducationStatus.ENROLLED)
    decision_b, note = evaluate_requirement(req, [], [bachelor])
    assert note is None and decision_b is not None
    assert decision_b.verdict == "covered"
    assert decision_b.criteria_refs == (bachelor.ref_id,)
    assert "优先条件未满足" in decision_b.reason

    master = _edu("岚江大学", DegreeLevel.MASTER, EducationStatus.ENROLLED)
    decision_m, _ = evaluate_requirement(req, [], [bachelor, master])
    assert decision_m is not None and decision_m.verdict == "covered"
    assert "优先条件未满足" not in decision_m.reason

    assoc = _edu("高职", DegreeLevel.ASSOCIATE, EducationStatus.COMPLETED)
    decision_a, _ = evaluate_requirement(req, [], [assoc])
    assert decision_a is not None and decision_a.verdict == "partial"
    assert "低于岗位要求" in decision_a.reason


def test_multiple_education_records_check_each_record_independently() -> None:
    """不能拼凑「本科已取得 + 硕士」或「本科在读 + 已取得硕士」满足「硕士在读」。"""
    req = Requirement(type=RequirementType.HARD, text="硕士在读可投")
    bachelor_enrolled = _edu("A 校", DegreeLevel.BACHELOR, EducationStatus.ENROLLED)
    master_completed = _edu("B 校", DegreeLevel.MASTER, EducationStatus.COMPLETED)

    mismatch, _ = evaluate_requirement(req, [], [bachelor_enrolled, master_completed])
    assert mismatch is not None and mismatch.verdict == "partial"

    master_enrolled = _edu("C 校", DegreeLevel.MASTER, EducationStatus.ENROLLED)
    matched, _ = evaluate_requirement(req, [], [bachelor_enrolled, master_enrolled])
    assert matched is not None and matched.verdict == "covered"
    assert matched.criteria_refs == (master_enrolled.ref_id,)


def test_ambiguous_compound_and_conflicting_requirements_trigger_review() -> None:
    compound = Requirement(type=RequirementType.HARD, text="本科及以上，熟悉 Python")
    dec, note = evaluate_requirement(
        compound, [], [_edu("A 校", DegreeLevel.BACHELOR, EducationStatus.ENROLLED)]
    )
    assert dec is None and note is not None and "复合" in note

    vague = Requirement(type=RequirementType.HARD, text="每周尽量多到岗")
    dec2, note2 = evaluate_requirement(vague, [_days(4)], [])
    assert dec2 is None and note2 is not None

    req_days = Requirement(type=RequirementType.HARD, text="每周至少到岗 3 天")
    basic = BasicFields(onsite_days_per_week="5天/周")
    dec3, note3 = evaluate_requirement(req_days, [_days(4)], [], basic_fields=basic)
    assert dec3 is None and note3 is not None and "不一致" in note3


def test_sync_main_gaps_removes_covered_and_adds_hard_gaps() -> None:
    r_days = Requirement(type=RequirementType.HARD, text="每周到岗 4 天以上")
    r_months = Requirement(type=RequirementType.HARD, text="实习至少 3 个月")
    d_days, _ = evaluate_requirement(r_days, [_days(5)], [])
    d_months, _ = evaluate_requirement(r_months, [], [])
    assert d_days is not None and d_months is not None

    synced = sync_main_gaps(
        ["缺少每周到岗 4 天证明", "缺少 Kafka 经验"],
        [r_days, r_months],
        {r_days.requirement_id: d_days, r_months.requirement_id: d_months},
    )
    assert all("到岗" not in g for g in synced)
    assert "缺少 Kafka 经验" in synced
    assert any("实习至少 3 个月" in g for g in synced)
