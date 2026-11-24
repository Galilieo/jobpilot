"""模拟模型已处理请求、业务运行却已终止的时序，验证迟到的产物整体回滚。"""

import json

import pytest
from sqlalchemy import select

from jobpilot.api import jobs, matching, resumes
from jobpilot.db.models import (
    AiRun,
    FactDraft,
    JobPosting,
    MatchReport,
    ResumeDocument,
    RunStatus,
)
from jobpilot.db.runs import set_run_status
from jobpilot.tests.test_facts_api import EXTRACTION, RESUME
from jobpilot.tests.test_jobs_api import JD, OUTPUT
from jobpilot.tests.test_match_api import _add_job, _publish_resume_facts, _sign_in


@pytest.mark.parametrize("kind", ["parse_resume", "parse_jd", "match"])
async def test_late_product_rolls_back(
    kind, async_client, model_outputs, session_factory, monkeypatch
):
    await _sign_in(async_client, "late@example.invalid")
    if kind == "match":
        library = await _publish_resume_facts(async_client, model_outputs)
        job = await _add_job(async_client, model_outputs)
        items = [
            {
                "requirement_id": req["requirement_id"],
                "verdict": "covered",
                "fact_ids": [library["facts"][0]["fact_id"]],
                "reason": "匹配",
                "suggestion": "保留",
            }
            for req in job["parsed"]["requirements"]
        ]
        module, attr, output = matching, "match_job", {"items": items}
        path, body = f"/api/jobs/{job['job_id']}/match", None
    elif kind == "parse_resume":
        module, attr, output = resumes, "extract_facts", EXTRACTION
        path, body = "/api/resumes", {"raw_text": RESUME}
    else:
        module, attr, output = jobs, "parse_job", OUTPUT
        path, body = "/api/jobs", {"raw_text": JD}
    original = getattr(module, attr)

    async def finish_elsewhere(factory, user_id, run_id, **kwargs):
        result = await original(factory, user_id, run_id, **kwargs)
        async with factory.begin() as db:
            assert await set_run_status(db, user_id, run_id, RunStatus.FAILED)
        return result

    monkeypatch.setattr(module, attr, finish_elsewhere)
    model_outputs.append(json.dumps(output))
    response = await async_client.post(path, json=body)
    assert (
        response.status_code == 409 and response.json()["detail"]["status"] == "failed"
    )
    async with session_factory() as db:
        run = (await db.scalars(select(AiRun).where(AiRun.kind == kind))).one()
        assert run.status == "failed"
        assert await db.scalar(select(MatchReport)) is None
        if kind == "parse_resume":
            assert (await db.scalar(select(ResumeDocument))).sections_json == []
            assert await db.scalar(select(FactDraft)) is None
        if kind == "parse_jd":
            assert await db.scalar(select(JobPosting)) is None
