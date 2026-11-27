"""用真实数据库和假上游验证付费 POST 的重放、并发和失败边界。"""

import asyncio
import json
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select

from jobpilot.api import jobs
from jobpilot.api.jobs import JobIn
from jobpilot.config import settings
from jobpilot.db.idempotency import Attempt, KeyScope, Operation, claim
from jobpilot.db.models import (
    AiRun,
    CallOutcome,
    FactDraft,
    IdempotencyKey,
    JobPosting,
    MatchReport,
    ModelCall,
    ResumeDocument,
    RunStatus,
)
from jobpilot.main import lifespan
from jobpilot.services import llm_gateway
from jobpilot.tests.test_facts_api import EXTRACTION, RESUME
from jobpilot.tests.test_jobs_api import JD, OUTPUT, sign_in
from jobpilot.tests.test_match_api import _add_job, _publish_resume_facts


def _headers(key="submit-once"):
    return {"Idempotency-Key": key}


async def _match_request(client, model_outputs):
    library = await _publish_resume_facts(client, model_outputs)
    job = await _add_job(client, model_outputs)
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
    model_outputs.append(json.dumps({"items": items, "main_gaps": []}))
    return f"/api/jobs/{job['job_id']}/match"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/resumes", {"raw_text": RESUME}),
        ("/api/jobs", {"raw_text": JD}),
        (f"/api/jobs/{uuid4()}/match", None),
    ],
)
async def test_key_is_required_after_authentication(async_client, path, body):
    assert (await async_client.post(path, json=body)).status_code == 401
    await sign_in(async_client, "key-required@example.invalid")
    assert (await async_client.post(path, json=body)).status_code == 400


async def test_resume_replays_original_revision_and_items(
    async_client, model_outputs, session_factory
):
    await sign_in(async_client, "key-resume@example.invalid")
    model_outputs.append(json.dumps(EXTRACTION))
    body = {"raw_text": RESUME, "filename": "简历.txt"}
    first = await async_client.post("/api/resumes", json=body, headers=_headers())
    assert first.status_code == 201, first.text
    data = first.json()
    item = data["items"][0]
    edited = await async_client.put(
        f"/api/facts/{item['lineage_id']}",
        json={"text": "从这里修改了草稿", "expected_revision": data["revision"]},
    )
    assert edited.json()["revision"] == data["revision"] + 1
    replay = await async_client.post(
        "/api/resumes",
        json={"filename": body["filename"], "raw_text": RESUME},
        headers=_headers(),
    )
    assert (replay.status_code, replay.content) == (201, first.content)
    mismatch = await async_client.post(
        "/api/resumes",
        json={**body, "raw_text": RESUME + "新增内容"},
        headers=_headers(),
    )
    assert mismatch.status_code == 422

    async with session_factory() as db:
        doc = (await db.scalars(select(ResumeDocument))).one()
        call = (await db.scalars(select(ModelCall))).one()
        key = (await db.scalars(select(IdempotencyKey))).one()
        draft = (await db.scalars(select(FactDraft))).one()
        assert len(draft.items_json) == len(data["items"]) == 2
        assert key.result_id == doc.doc_id == UUID(data["doc_id"])
        assert key.run_id == call.run_id == UUID(data["run_id"])
        assert key.response_body == first.text


async def test_concurrent_same_job_key_returns_409_and_calls_model_once(
    async_client, model_outputs, session_factory, monkeypatch
):
    await sign_in(async_client, "key-concurrency@example.invalid")
    model_outputs.append(json.dumps(OUTPUT))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = jobs.parse_job

    async def paused_model(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(jobs, "parse_job", paused_model)
    body = {"raw_text": JD}
    task = asyncio.create_task(
        async_client.post("/api/jobs", json=body, headers=_headers())
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with session_factory() as db:
            reservation = (await db.scalars(select(IdempotencyKey))).one()
            run = (await db.scalars(select(AiRun))).one()
            assert reservation.status == "processing"
            assert reservation.run_id == run.run_id
        parallel = await asyncio.wait_for(
            async_client.post("/api/jobs", json=body, headers=_headers()), 3
        )
        assert parallel.status_code == 409
    finally:
        release.set()
        first = await task
    replay = await async_client.post("/api/jobs", json=body, headers=_headers())
    assert first.status_code == 201
    assert (replay.status_code, replay.content) == (201, first.content)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(ModelCall)) == 1
        assert await db.scalar(select(func.count()).select_from(JobPosting)) == 1


async def test_job_content_dedup_saves_200_and_old_personal_fields(
    async_client, model_outputs, session_factory
):
    await sign_in(async_client, "key-job-dedup@example.invalid")
    model_outputs.append(json.dumps(OUTPUT))
    body = {"raw_text": JD}
    created = await async_client.post(
        "/api/jobs", json=body, headers=_headers("create")
    )
    assert created.status_code == 201, created.text
    duplicate = await async_client.post("/api/jobs", json=body, headers=_headers())
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == created.json()["job_id"]
    updated = await async_client.put(
        f"/api/jobs/{created.json()['job_id']}", json={"note": "最新备注"}
    )
    assert updated.is_success
    replay = await async_client.post("/api/jobs", json=body, headers=_headers())
    assert (replay.status_code, replay.content) == (200, duplicate.content)
    async with session_factory() as db:
        reservation = await db.scalar(
            select(IdempotencyKey).filter_by(key="submit-once")
        )
        assert reservation.run_id is None
        assert await db.scalar(select(func.count()).select_from(ModelCall)) == 1


async def test_losing_job_insert_saves_200_for_its_own_run(
    async_client, model_outputs, session_factory, monkeypatch
):
    await sign_in(async_client, "key-job-race@example.invalid")
    model_outputs.extend([json.dumps(OUTPUT)] * 2)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = jobs.parse_job

    async def pause_first(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(jobs, "parse_job", pause_first)
    body = {"raw_text": JD}
    pending = asyncio.create_task(
        async_client.post("/api/jobs", json=body, headers=_headers("paused"))
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        winner = await async_client.post("/api/jobs", json=body, headers=_headers())
    finally:
        release.set()
        loser = await pending
    assert winner.status_code == 201
    assert (loser.status_code, loser.content) == (200, winner.content)
    replay = await async_client.post("/api/jobs", json=body, headers=_headers("paused"))
    assert (replay.status_code, replay.content) == (200, loser.content)
    async with session_factory() as db:
        key = await db.scalar(select(IdempotencyKey).filter_by(key="paused"))
        attempt = await db.get(AiRun, key.run_id)
        assert attempt.status == RunStatus.CANCELLED
        assert await db.scalar(select(func.count()).select_from(JobPosting)) == 1
    job_id = winner.json()["job_id"]
    assert (await async_client.delete(f"/api/jobs/{job_id}")).status_code == 204
    model_outputs.append(json.dumps(OUTPUT))
    retired = await async_client.post(
        "/api/jobs", json=body, headers=_headers("paused")
    )
    assert retired.status_code == 410
    assert len(model_outputs) == 1
    reimported = await async_client.post(
        "/api/jobs", json=body, headers=_headers("fresh")
    )
    assert reimported.status_code == 201
    assert reimported.json()["job_id"] != job_id


async def test_delete_job_retires_original_and_ordinary_duplicate(
    async_client, model_outputs, session_factory
):
    await sign_in(async_client, "key-job-delete@example.invalid")
    model_outputs.extend([json.dumps(OUTPUT)] * 2)
    body = {"raw_text": JD}
    created = await async_client.post("/api/jobs", json=body, headers=_headers("first"))
    duplicate = await async_client.post(
        "/api/jobs", json=body, headers=_headers("reuse")
    )
    assert created.status_code == 201 and duplicate.status_code == 200
    job_id = duplicate.json()["job_id"]
    assert (await async_client.delete(f"/api/jobs/{job_id}")).status_code == 204
    for key in ("first", "reuse"):
        retired = await async_client.post("/api/jobs", json=body, headers=_headers(key))
        assert retired.status_code == 410
        assert retired.json()["detail"] == "这条记录已经删除，如需重新记录请重新提交"
    assert len(model_outputs) == 1
    async with session_factory() as db:
        keys = list(await db.scalars(select(IdempotencyKey)))
        assert len(keys) == 2
        assert all(
            key.status == "retired" and key.response_body is None for key in keys
        )
        assert all(key.response_status is None for key in keys)
    changed = await async_client.post(
        "/api/jobs", json={"raw_text": "其他岗位"}, headers=_headers("first")
    )
    assert changed.status_code == 422
    reimported = await async_client.post(
        "/api/jobs", json=body, headers=_headers("fresh")
    )
    assert reimported.status_code == 201
    assert reimported.json()["job_id"] != job_id


async def test_claim_key_removed_between_insert_and_select_returns_conflict(
    async_client, session_factory, monkeypatch
):
    await sign_in(async_client, "lost-idempotency-key@example.invalid")
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    scope = KeyScope(user_id, Operation.PARSE_JD, "removed")
    request_hash = "a" * 64
    async with session_factory.begin() as db:
        await claim(db, scope, request_hash)
    async with session_factory.begin() as db:
        original = db.scalars

        async def remove_key_before_read(*args, **kwargs):
            async with session_factory.begin() as other:
                await other.execute(delete(IdempotencyKey).filter_by(**scope.columns()))
            return await original(*args, **kwargs)

        monkeypatch.setattr(db, "scalars", remove_key_before_read)
        async with asyncio.timeout(3):
            assert await claim(db, scope, request_hash) == "missing"


async def test_match_replays_report_and_checks_job_path(
    async_client, model_outputs, session_factory
):
    await sign_in(async_client, "key-match@example.invalid")
    path = await _match_request(async_client, model_outputs)
    report = await async_client.post(path, headers=_headers())
    assert report.status_code == 201, report.text
    different = await _add_job(async_client, model_outputs, raw_text="另一个岗位")
    mismatch = await async_client.post(
        f"/api/jobs/{different['job_id']}/match", headers=_headers()
    )
    assert mismatch.status_code == 422
    updated = await async_client.put(
        path.removesuffix("/match"), json={"requirements": []}
    )
    assert updated.is_success
    replay = await async_client.post(path, headers=_headers())
    assert (replay.status_code, replay.content) == (201, report.content)

    async with session_factory() as db:
        reservation = await db.scalar(
            select(IdempotencyKey).filter_by(operation="match")
        )
        stored = (await db.scalars(select(MatchReport))).one()
        assert reservation.result_id == stored.report_id
        assert stored.report_id == UUID(report.json()["report_id"])
        assert reservation.run_id == stored.run_id == UUID(report.json()["run_id"])
        calls = select(func.count()).select_from(ModelCall).filter_by(purpose="match")
        assert await db.scalar(calls) == 1


async def test_failed_resume_same_key_really_runs_again_with_original_document(
    async_client, model_outputs, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "model_retry_delay_seconds", 0)
    await sign_in(async_client, "key-retry@example.invalid")
    model_outputs.extend(["invalid", "still invalid"])
    body = {"raw_text": RESUME}
    failed = await async_client.post("/api/resumes", json=body, headers=_headers())
    assert failed.status_code == 502
    async with session_factory() as db:
        reservation = (await db.scalars(select(IdempotencyKey))).one()
        failed_run = await db.get(AiRun, reservation.run_id)
        assert reservation.status == "failed"
        assert failed_run.status == RunStatus.FAILED
        doc_id = reservation.result_id
        assert reservation.response_body is None

    model_outputs.append(json.dumps(EXTRACTION))
    retried = await async_client.post("/api/resumes", json=body, headers=_headers())
    assert retried.status_code == 201, retried.text
    assert retried.json()["doc_id"] == str(doc_id)
    assert retried.json()["run_id"] != str(failed_run.run_id)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(ResumeDocument)) == 1
        calls = await db.scalars(
            select(ModelCall).where(ModelCall.run_id != failed_run.run_id)
        )
        assert calls.one().outcome == CallOutcome.SUCCESS


@pytest.mark.parametrize("operation", list(Operation))
async def test_snapshot_failure_rolls_back_product_and_success(
    operation, async_client, model_outputs, session_factory, monkeypatch
):
    await sign_in(async_client, "key-atomic@example.invalid")
    if operation is Operation.MATCH:
        path, body = await _match_request(async_client, model_outputs), None
    elif operation is Operation.PARSE_RESUME:
        path, body = "/api/resumes", {"raw_text": RESUME}
        model_outputs.append(json.dumps(EXTRACTION))
    else:
        path, body = "/api/jobs", {"raw_text": JD}
        model_outputs.append(json.dumps(OUTPUT))

    async def fail_snapshot(*args, **kwargs):
        raise RuntimeError("快照落库失败")

    monkeypatch.setattr(Attempt, "succeed", fail_snapshot)
    response = await async_client.post(path, json=body, headers=_headers())
    assert response.status_code == 500
    assert response.json() == {"detail": "服务器内部错误"}
    async with session_factory() as db:
        reservation = await db.scalar(
            select(IdempotencyKey).filter_by(key="submit-once")
        )
        run = await db.get(AiRun, reservation.run_id)
        assert reservation.status == "failed" and reservation.response_body is None
        assert run.status == RunStatus.FAILED
        assert await db.scalar(select(MatchReport)) is None
        if operation is Operation.PARSE_RESUME:
            doc = await db.get(ResumeDocument, reservation.result_id)
            assert doc.sections_json == []
            assert await db.scalar(select(FactDraft)) is None
        if operation is Operation.PARSE_JD:
            assert await db.scalar(select(JobPosting)) is None


async def test_cancelled_request_frees_its_key_and_keeps_the_call_unknown(
    async_client, model_outputs, session_factory, monkeypatch
):
    """等模型时请求协程被取消：这次尝试已经退出，键标为失败，同一个键可以重送。

    这条测试原来断言键停在 processing、重送回 409。那样键一直占着，同一个键
    要等服务重启才回收（重启时 fail_interrupted_keys 做的也是标为失败）。
    模型调用可能已经计费，所以结论保持 unknown：明确重试会再调一次模型。
    """
    await sign_in(async_client, "key-interrupted@example.invalid")
    entered = asyncio.Event()

    async def interrupted(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    create_completion = llm_gateway._create_completion
    monkeypatch.setattr(llm_gateway, "_create_completion", interrupted)
    body = {"raw_text": JD}
    task = asyncio.create_task(
        async_client.post("/api/jobs", json=body, headers=_headers())
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with session_factory() as db:
        reservation = (await db.scalars(select(IdempotencyKey))).one()
        call = (await db.scalars(select(ModelCall))).one()
        run = await db.get(AiRun, reservation.run_id)
        assert reservation.status == "failed"
        assert reservation.run_id == call.run_id == run.run_id
        assert run.status == RunStatus.FAILED
        assert call.outcome == CallOutcome.UNKNOWN
        assert call.prompt_tokens is None and call.completion_tokens is None

    monkeypatch.setattr(llm_gateway, "_create_completion", create_completion)
    model_outputs.append(json.dumps(OUTPUT))
    retried = await async_client.post("/api/jobs", json=body, headers=_headers())
    assert retried.status_code == 201, retried.text
    async with session_factory() as db:
        reservation = (await db.scalars(select(IdempotencyKey))).one()
        assert reservation.status == "succeeded"  # 重送用的是同一个键
        assert reservation.run_id != run.run_id  # 但是新的一次运行
        outcomes = (await db.scalars(select(ModelCall.outcome))).all()
        assert outcomes.count(CallOutcome.SUCCESS) == 1
        assert await db.scalar(select(func.count()).select_from(JobPosting)) == 1


async def test_service_restart_recovers_processing_without_touching_success(
    async_client, model_outputs, session_factory, monkeypatch
):
    await sign_in(async_client, "key-restart@example.invalid")
    first = await async_client.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=_headers()
    )
    assert first.status_code == 201
    payload = JobIn(raw_text=JD)
    digest = sha256(payload.model_dump_json().encode()).hexdigest()
    async with session_factory.begin() as db:
        completed = (await db.scalars(select(IdempotencyKey))).one()
        scope = KeyScope(completed.user_id, Operation.PARSE_JD, "interrupted")
        assert isinstance(await claim(db, scope, digest), Attempt)
        bound_scope = KeyScope(completed.user_id, Operation.PARSE_JD, "bound-run")
        assert isinstance(await claim(db, bound_scope, digest), Attempt)
        run = AiRun(user_id=completed.user_id, kind=Operation.PARSE_JD)
        db.add(run)
        await db.flush()
        bound = await db.get(IdempotencyKey, bound_scope.columns())
        bound.run_id = run.run_id
        db.add(
            ModelCall(
                user_id=run.user_id,
                run_id=run.run_id,
                purpose="parse_jd",
                model="假上游",
                outcome=CallOutcome.UNKNOWN,
            )
        )

    monkeypatch.setattr(settings, "postgres_db", settings.postgres_test_db)
    async with lifespan(FastAPI()):
        pass

    async with session_factory() as db:
        interrupted = await db.get(IdempotencyKey, scope.columns())
        assert interrupted.status == "failed" and interrupted.run_id is None
        bound = await db.get(IdempotencyKey, bound_scope.columns())
        assert bound.status == "failed" and bound.run_id == run.run_id
        recovered_run = await db.get(AiRun, bound.run_id)
        assert recovered_run.status == RunStatus.FAILED
        call = await db.scalar(select(ModelCall).filter_by(run_id=run.run_id))
        assert call.outcome == CallOutcome.UNKNOWN
        assert call.prompt_tokens is None and call.completion_tokens is None
    replay = await async_client.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=_headers()
    )
    assert (replay.status_code, replay.content) == (201, first.content)
    retried = await async_client.post(
        "/api/jobs", json=payload.model_dump(), headers=_headers("interrupted")
    )
    assert retried.status_code == 201, retried.text


async def test_same_key_is_scoped_to_owner_and_operation(async_client, model_outputs):
    await sign_in(async_client, "key-owner-a@example.invalid")
    resume = await async_client.post(
        "/api/resumes", json={"raw_text": RESUME}, headers=_headers()
    )
    job_a = await async_client.post(
        "/api/jobs", json={"raw_text": JD}, headers=_headers()
    )
    assert (resume.status_code, job_a.status_code) == (201, 201)
    await async_client.post("/api/auth/logout")
    await sign_in(async_client, "key-owner-b@example.invalid")
    job_b = await async_client.post(
        "/api/jobs", json={"raw_text": JD}, headers=_headers()
    )
    assert job_b.status_code == 201
    assert job_b.json()["job_id"] != job_a.json()["job_id"]
    denial = await async_client.post(
        f"/api/jobs/{job_a.json()['job_id']}/match", headers=_headers()
    )
    assert denial.status_code == 404
