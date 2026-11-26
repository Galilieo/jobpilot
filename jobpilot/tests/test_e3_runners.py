"""验证 E3 标注桥接、计分和盲标规则。只有网关端到端验证使用本机测试库。"""

import json
from random import Random
from uuid import uuid4

from evals.runners.e1_extract_facts import eval_user_id
from evals.runners.e3_dataset import (
    load_job,
    load_labels,
    load_resume,
    load_split,
    pair_id,
)
from evals.runners.e3_match import run_pair
from evals.runners.e3_score import Attempt, Prediction, score_attempts
from evals.runners.e3_second_annotator import (
    _sample_split,
    _sheet_row,
    _stratum,
    agreement,
)
from evals.runners.e4_ground_check import fact_id
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.services.matching import ModelMatchVerdict
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream


def test_e3_inputs_reuse_fact_ids_and_keep_condition_provenance() -> None:
    user_id = uuid4()
    assert len(load_labels("dev")) == 200
    for split in ("dev", "test"):
        resumes, jobs = load_split(split)
        for resume_id in resumes:
            resume = load_resume(resume_id, user_id)
            assert resume.inputs.facts[0].fact_id == fact_id(resume_id, 0)
            assert resume.evidence_ids[str(fact_id(resume_id, 0))] == 0
            assert len(resume.evidence_ids) == (
                len(resume.inputs.facts)
                + len(resume.inputs.criteria)
                + len(resume.inputs.education)
            )
        assert all(
            req.quote_verified
            for job in jobs
            for req in load_job(job).snapshot.requirements
        )


async def test_e3_calls_real_match_service_with_mock_transport(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await eval_user_id(session_factory)
    resume = load_resume("resume-011", user_id)
    job = load_job("jd-syn-01")
    # 假上游一律回答未覆盖，让现有程序用 4 天和 4 个月的标注覆写结论。
    raw = {
        "items": [
            {
                "requirement_id": str(requirement_id),
                "verdict": "missing",
                "reason": "假模型未引用条件。",
                "suggestion": "检查发布的事实。",
            }
            for requirement_id in job.requirement_ids
        ]
    }
    async with fake_upstream(
        lambda request: completion_response(json.dumps(raw))
    ) as client:
        record = await run_pair(session_factory, user_id, client, resume, job)
    items = {item["requirement_id"]: item for item in record["items"]}
    assert record["error"] is None
    assert len(record["calls"]) == 1
    assert items["jd-syn-01-r01"]["verdict"] == "missing"
    assert items["jd-syn-01-r04"]["verdict"] == "covered"
    assert items["jd-syn-01-r04"]["decision_source"] == "program"
    assert items["jd-syn-01-r04"]["evidence"] == ["criteria.onsite_days_per_week"]


def test_e3_score_counts_evidence_and_failed_or_unstable_runs() -> None:
    target = pair_id("resume-001", "jd-syn-01")
    labels = [label for label in load_labels("dev") if label.pair_id == target]
    correct = [
        Prediction(
            requirement_id=label.requirement_id,
            verdict=label.verdict,
            decision_source="model",
            evidence=label.evidence,
            reason="模拟结果",
        )
        for label in labels
    ]
    modified = list(correct)
    modified[0] = correct[0].model_copy(update={"evidence": [*correct[0].evidence, 10]})
    modified[1] = correct[1].model_copy(
        update={"verdict": ModelMatchVerdict.PARTIAL, "evidence": [2]}
    )
    attempts = [
        Attempt(run_index=1, pair_id=target, error=None, items=correct),
        Attempt(run_index=2, pair_id=target, error=None, items=modified),
        Attempt(run_index=3, pair_id=target, error="APIConnectionError", items=[]),
    ]
    score = score_attempts(labels, attempts, 3)
    assert score.overall["总计"].correct == 15
    assert score.overall["总计"].count == 16
    assert score.overall["模型"].evidence_hits == 9
    assert score.overall["模型"].evidence_extra == 1
    assert score.failed["APIConnectionError"] == 1
    assert sum(len(set(values)) > 1 for values in score.verdicts.values()) == 1


def test_e3_blind_sheet_covers_each_dimension_and_removes_verdicts() -> None:
    labels = load_labels("dev")
    sample = _sample_split(labels, Random(42), 25)
    classes = [_stratum(label) for label in sample]
    assert {row[0] for row in classes} == {"covered", "partial", "missing"}
    assert {row[1] for row in classes} == {"hard", "preferred"}
    assert {row[2] for row in classes} == {"program", "model"}
    row = _sheet_row("E3-01", sample[0])
    assert row["判断"] == ""
    assert set(row) == {
        "sample_id",
        "要求原文",
        "类型",
        "E1 事实与原文",
        "求职条件",
        "教育",
        "判断",
    }


def test_e3_kappa_adjusts_for_chance_matches() -> None:
    covered, partial = ModelMatchVerdict.COVERED, ModelMatchVerdict.PARTIAL
    assert agreement([(covered, covered), (partial, partial)]) == (1.0, 1.0)
    assert agreement([(covered, covered), (partial, covered)]) == (0.5, 0.0)
    assert agreement([(covered, covered), (covered, covered)]) == (1.0, None)
