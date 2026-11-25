"""匹配服务纯函数测试：表达缺口判定、事实归属/版本校验、硬性与加分项分别汇总。

不连接数据库，直接验证 build_match_report、parse_match_output 与 format_match_messages。
"""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jobpilot.db.models import CriterionField, FactKind, FactSource, Responsibility
from jobpilot.services.criteria_match import CriterionCandidate
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.jobs import BasicFields, ParsedJob, Requirement, RequirementType
from jobpilot.services.matching import (
    FactCandidate,
    MatchValidationError,
    MatchVerdict,
    RawMatchOutput,
    build_match_report,
    format_match_messages,
    parse_match_output,
)


def _req(req_type: RequirementType, text: str) -> Requirement:
    return Requirement(type=req_type, text=text)


def _fact(
    user_id: UUID,
    fact_set_id: UUID,
    *,
    source: FactSource = FactSource.RESUME,
    confirmed: bool = True,
    text: str = "用 Python 实现服务",
) -> FactCandidate:
    return FactCandidate(
        fact_id=uuid4(),
        user_id=user_id,
        fact_set_id=fact_set_id,
        lineage_id=uuid4(),
        experience="实习经历",
        kind=FactKind.ACTION,
        responsibility=Responsibility.OWN,
        text=text,
        source=source,
        confirmed=confirmed,
    )


def _raw(
    items: list[dict[str, Any]], main_gaps: list[str] | None = None
) -> RawMatchOutput:
    return RawMatchOutput.model_validate({"items": items, "main_gaps": main_gaps or []})


def test_build_match_report_reclassifies_expression_gap_and_summarizes() -> None:
    """纯用户补充的 covered 改判为 expression_gap；硬性与加分项分开汇总。"""
    user_id, fact_set_id = uuid4(), uuid4()
    r1 = _req(RequirementType.HARD, "熟悉 Python")
    r2 = _req(RequirementType.HARD, "熟悉 Redis 缓存")
    r3 = _req(RequirementType.HARD, "熟悉 Go 与 MySQL")
    r4 = _req(RequirementType.PREFERRED, "有分布式压测经验")
    r5 = _req(RequirementType.PREFERRED, "有 Kafka 经验")

    resume_py = _fact(
        user_id, fact_set_id, source=FactSource.RESUME, text="用 Python 写接口"
    )
    user_redis = _fact(
        user_id, fact_set_id, source=FactSource.USER, text="用 Redis 做分布式锁"
    )
    user_redis_2 = _fact(
        user_id, fact_set_id, source=FactSource.USER, text="做过 Redis 缓存穿透防护"
    )
    resume_go = _fact(
        user_id, fact_set_id, source=FactSource.RESUME, text="用 Go 写服务"
    )
    user_loadtest = _fact(
        user_id, fact_set_id, source=FactSource.USER, text="做过单机接口压测"
    )

    # 故意打乱模型返回顺序，且重复引用同一 fact_id
    raw = _raw(
        [
            {
                "requirement_id": r5.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "reason": "没有 Kafka 经历",
                "suggestion": "建议学习并补做相关项目",
            },
            {
                "requirement_id": r4.requirement_id,
                "verdict": "partial",
                "fact_ids": [user_loadtest.fact_id],
                "reason": "仅做过单机压测，尚未涉及分布式压测",
                "suggestion": "可补充分布式压测实践",
            },
            {
                "requirement_id": r3.requirement_id,
                "verdict": "covered",
                "fact_ids": [resume_go.fact_id, user_redis.fact_id],
                "reason": "混有简历事实和用户补充",
                "suggestion": "继续突出 Go 实践",
            },
            {
                "requirement_id": r2.requirement_id,
                "verdict": "covered",
                "fact_ids": [
                    user_redis.fact_id,
                    user_redis.fact_id,
                    user_redis_2.fact_id,
                ],
                "reason": "做过 Redis 缓存，但原简历没写",
                "suggestion": "写进简历",
            },
            {
                "requirement_id": r1.requirement_id,
                "verdict": "covered",
                "fact_ids": [resume_py.fact_id],
                "reason": "简历已写出",
                "suggestion": "保留",
            },
        ],
        main_gaps=["缺少 Kafka 经验"],
    )

    built = build_match_report(
        user_id=user_id,
        fact_set_id=fact_set_id,
        requirements=[r1, r2, r3, r4, r5],
        facts=[resume_py, user_redis, user_redis_2, resume_go, user_loadtest],
        raw=raw,
    )

    assert [item.requirement_id for item in built.items] == [
        r1.requirement_id,
        r2.requirement_id,
        r3.requirement_id,
        r4.requirement_id,
        r5.requirement_id,
    ]
    verdicts = {item.requirement_id: item.verdict for item in built.items}
    assert verdicts[r1.requirement_id] is MatchVerdict.COVERED
    # 纯 user 来源的 covered -> expression_gap，且重复 fact_ids 去重保留顺序
    assert verdicts[r2.requirement_id] is MatchVerdict.EXPRESSION_GAP
    assert built.items[1].fact_ids == [user_redis.fact_id, user_redis_2.fact_id]
    # 混合 resume + user 的 covered 保持 covered
    assert verdicts[r3.requirement_id] is MatchVerdict.COVERED
    # 纯 user 来源的 partial 保持 partial，不改判为 expression_gap
    assert verdicts[r4.requirement_id] is MatchVerdict.PARTIAL
    assert verdicts[r5.requirement_id] is MatchVerdict.MISSING

    assert built.hard_summary.model_dump() == {
        "total": 3,
        "covered": 2,
        "expression_gap": 1,
        "partial": 0,
        "missing": 0,
    }
    assert built.preferred_summary.model_dump() == {
        "total": 2,
        "covered": 0,
        "expression_gap": 0,
        "partial": 1,
        "missing": 1,
    }
    assert built.main_gaps == ["缺少 Kafka 经验"]


def test_build_match_report_rejects_bad_fact_references() -> None:
    """不存在、跨用户、同用户其他版本、未确认的事实全部拒绝。"""
    user_id, other_user, set_v1, set_v2 = uuid4(), uuid4(), uuid4(), uuid4()
    req = _req(RequirementType.HARD, "熟悉 Python")
    valid = _fact(user_id, set_v2)
    other_users_fact = _fact(other_user, set_v2)
    old_version_fact = _fact(user_id, set_v1)
    unconfirmed_fact = _fact(user_id, set_v2, confirmed=False)

    bad_candidates = [
        ("missing-id", uuid4()),
        ("other-user", other_users_fact.fact_id),
        ("other-version", old_version_fact.fact_id),
        ("unconfirmed", unconfirmed_fact.fact_id),
    ]
    for label, bad_id in bad_candidates:
        raw = _raw(
            [
                {
                    "requirement_id": req.requirement_id,
                    "verdict": "covered",
                    "fact_ids": [bad_id],
                    "reason": "理由",
                    "suggestion": "建议",
                }
            ]
        )
        with pytest.raises(MatchValidationError, match="事实 ID") as exc_info:
            build_match_report(
                user_id=user_id,
                fact_set_id=set_v2,
                requirements=[req],
                facts=[valid, other_users_fact, old_version_fact, unconfirmed_fact],
                raw=raw,
            )
        assert exc_info.value, label


@pytest.mark.parametrize(
    ("verdict", "use_fact"),
    [("covered", False), ("partial", False), ("missing", True)],
    ids=["covered-no-fact", "partial-no-fact", "missing-with-fact"],
)
def test_build_match_report_rejects_verdict_fact_mismatch(
    verdict: str, use_fact: bool
) -> None:
    user_id, fact_set_id = uuid4(), uuid4()
    req = _req(RequirementType.HARD, "熟悉 Python")
    fact = _fact(user_id, fact_set_id)
    raw = _raw(
        [
            {
                "requirement_id": req.requirement_id,
                "verdict": verdict,
                "fact_ids": [fact.fact_id] if use_fact else [],
                "reason": "理由",
                "suggestion": "建议",
            }
        ]
    )

    with pytest.raises(MatchValidationError):
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=[req],
            facts=[fact],
            raw=raw,
        )


def test_build_match_report_requires_exact_requirement_coverage() -> None:
    """遗漏要求、未知要求 ID、重复要求 ID 均报错。"""
    user_id, fact_set_id = uuid4(), uuid4()
    r1 = _req(RequirementType.HARD, "要求一")
    r2 = _req(RequirementType.HARD, "要求二")
    fact = _fact(user_id, fact_set_id)

    missing_raw = _raw(
        [
            {
                "requirement_id": r1.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "reason": "缺",
                "suggestion": "学",
            }
        ]
    )
    with pytest.raises(MatchValidationError, match="遗漏"):
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=[r1, r2],
            facts=[fact],
            raw=missing_raw,
        )

    unknown_raw = _raw(
        [
            {
                "requirement_id": r1.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "reason": "缺",
                "suggestion": "学",
            },
            {
                "requirement_id": uuid4(),
                "verdict": "missing",
                "fact_ids": [],
                "reason": "缺",
                "suggestion": "学",
            },
        ]
    )
    with pytest.raises(MatchValidationError, match="未知"):
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=[r1, r2],
            facts=[fact],
            raw=unknown_raw,
        )

    duplicate_raw = _raw(
        [
            {
                "requirement_id": r1.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "reason": "缺",
                "suggestion": "学",
            },
            {
                "requirement_id": r1.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "reason": "缺",
                "suggestion": "学",
            },
        ]
    )
    with pytest.raises(MatchValidationError, match="重复"):
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=[r1, r2],
            facts=[fact],
            raw=duplicate_raw,
        )


def test_parse_match_output_rejects_truncation_expression_gap_and_score() -> None:
    """截断抛 TruncatedOutputError；模型自称 expression_gap 或加 score 字段均拒绝。"""
    truncated = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content='{"items": ['),
            )
        ]
    )
    with pytest.raises(TruncatedOutputError):
        parse_match_output(truncated)  # type: ignore[arg-type]

    rid = str(uuid4())
    for bad_payload in (
        {
            "items": [
                {
                    "requirement_id": rid,
                    "verdict": "expression_gap",
                    "fact_ids": [str(uuid4())],
                    "reason": "x",
                    "suggestion": "y",
                }
            ],
            "main_gaps": [],
        },
        {"items": [], "main_gaps": [], "score": 87},
    ):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(bad_payload)),
                )
            ]
        )
        with pytest.raises(ValidationError):
            parse_match_output(completion)  # type: ignore[arg-type]


def test_format_match_messages_wraps_untrusted_blocks_and_hides_source() -> None:
    """发送给模型的消息包装在不可信区块中，且不包含事实 source 字段。"""
    user_id, fact_set_id = uuid4(), uuid4()
    req = _req(RequirementType.HARD, "熟悉 Python")
    snapshot = ParsedJob(
        basic_fields=BasicFields(title="后端开发实习生"),
        requirements=[req],
        duties=[],
        keywords=["Python"],
    )
    fact = _fact(user_id, fact_set_id, source=FactSource.USER, text="用 FastAPI 写接口")

    messages = format_match_messages(snapshot, [fact])

    assert len(messages) == 2
    user_msg = messages[1]["content"]
    assert isinstance(user_msg, str)
    assert '<untrusted_data label="job_requirements">' in user_msg
    assert '<untrusted_data label="facts">' in user_msg
    assert '<untrusted_data label="criteria">' in user_msg
    assert str(req.requirement_id) in user_msg
    assert str(fact.fact_id) in user_msg
    assert '"source"' not in user_msg


def test_program_overrides_model_missing_and_criteria_never_expression_gap() -> None:
    """程序覆盖模型 missing；引用条件不改判表达缺口；拒绝未知条件引用。"""
    user_id, fact_set_id = uuid4(), uuid4()
    r_days = _req(RequirementType.HARD, "每周至少到岗 4 天")
    r_compound = _req(RequirementType.HARD, "每周可到岗 4 天以上，熟悉 Python")
    crit = CriterionCandidate(
        ref_id="criteria.onsite_days_per_week",
        field=CriterionField.ONSITE_DAYS_PER_WEEK,
        int_value=5,
        text_value=None,
        text="每周可到岗 5 天",
    )
    user_fact = _fact(
        user_id, fact_set_id, source=FactSource.USER, text="用 Python 写服务"
    )
    raw = _raw(
        [
            {
                "requirement_id": r_days.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "criteria_refs": [],
                "reason": "未看到到岗天数",
                "suggestion": "请补充",
            },
            {
                "requirement_id": r_compound.requirement_id,
                "verdict": "covered",
                "fact_ids": [user_fact.fact_id],
                "criteria_refs": [crit.ref_id],
                "reason": "满足天数和 Python",
                "suggestion": "突出 Python",
            },
        ],
        main_gaps=["缺少每周到岗 4 天说明"],
    )

    built = build_match_report(
        user_id=user_id,
        fact_set_id=fact_set_id,
        requirements=[r_days, r_compound],
        facts=[user_fact],
        criteria=[crit],
        raw=raw,
    )

    days_item, compound_item = built.items
    assert days_item.verdict is MatchVerdict.COVERED
    assert days_item.decision_source == "program"
    assert days_item.criteria_refs == [crit.ref_id]
    assert built.main_gaps == []

    # 引用了求职条件：即使 fact 全部来自用户，也不改判为 expression_gap。
    assert compound_item.verdict is MatchVerdict.COVERED
    assert compound_item.decision_source == "model"
    assert compound_item.review_note is not None and "复合" in compound_item.review_note

    bad_raw = _raw(
        [
            {
                "requirement_id": r_days.requirement_id,
                "verdict": "covered",
                "fact_ids": [],
                "criteria_refs": ["criteria.unknown"],
                "reason": "x",
                "suggestion": "y",
            },
            {
                "requirement_id": r_compound.requirement_id,
                "verdict": "missing",
                "fact_ids": [],
                "criteria_refs": [],
                "reason": "x",
                "suggestion": "y",
            },
        ]
    )
    with pytest.raises(MatchValidationError, match="条件 ID"):
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=[r_days, r_compound],
            facts=[user_fact],
            criteria=[crit],
            raw=bad_raw,
        )
