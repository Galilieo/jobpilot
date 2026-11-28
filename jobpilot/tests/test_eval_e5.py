"""用假上游验证 E5 的五组输出、记账和产品图接线。"""

import json
import re

import httpx
import pytest
from evals.runners.e5_dataset import pairs_for
from evals.runners.e5_run import Version, _run_pair, eval_prompt
from evals.runners.e5_store import Factory

from jobpilot.db.models import CheckStatus, RunStatus
from jobpilot.services.prompts import load_prompt
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream


def _block(content: str, label: str) -> dict | list:
    match = re.search(
        rf'<untrusted_data label="{label}">\n(.*?)\n</untrusted_data>',
        content,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match[1])


def _fake_model(request: httpx.Request) -> httpx.Response:
    system, user = [row["content"] for row in json.loads(request.content)["messages"]]
    if system.startswith(eval_prompt("e5_a").text):
        result = {"entries": [{"experience": "项目", "bullets": ["测试句子"]}]}
    elif system == load_prompt("match_job").text:
        job = _block(user, "job_requirements")
        result = {
            "items": [
                {
                    "requirement_id": req["requirement_id"],
                    "verdict": "missing",
                    "reason": "假模型未匹配",
                    "suggestion": "保持原样",
                }
                for req in job["requirements"]
            ]
        }
    elif system == load_prompt("rewrite_bullets").text:
        first = _block(user, "facts")[0]
        result = {"bullets": [{"text": first["text"], "fact_ids": [first["id"]]}]}
    elif system == load_prompt("revise_bullet").text:
        first = _block(user, "facts")[0]
        result = {"text": first["text"], "fact_ids": [first["id"]]}
    else:
        assert system == load_prompt("ground_check").text
        result = {"verdict": "supported", "unsupported_spans": []}
    return completion_response(json.dumps(result, ensure_ascii=False))


@pytest.mark.parametrize("version", list(Version))
async def test_five_versions_record_final_bullets_and_model_calls(
    session_factory: Factory, version: Version
) -> None:
    pair = pairs_for("dev", ["resume-011__jd-syn-03"])[0]

    async with fake_upstream(_fake_model) as model:
        attempt = await _run_pair(session_factory, model, pair, version)

    assert attempt.status is RunStatus.SUCCEEDED
    assert attempt.bullets and all(bullet.text for bullet in attempt.bullets)
    purposes = [call["purpose"] for call in attempt.calls]
    if version in (Version.A, Version.A2):
        assert purposes == ["rewrite"]
        assert not attempt.bullets[0].fact_ids
        return
    assert purposes.count("match") == 1
    assert all(
        bullet.fact_ids and set(bullet.fact_ids) <= pair.facts.keys()
        for bullet in attempt.bullets
    )
    assert ("ground_check" in purposes) == (version is Version.D)
    if version is Version.B:
        assert all(bullet.check_status is None for bullet in attempt.bullets)
    else:
        assert {bullet.check_status for bullet in attempt.bullets} <= {
            CheckStatus.PASSED,
            CheckStatus.FALLBACK,
        }


async def test_checked_groups_reuse_the_same_match_and_drafts(
    session_factory: Factory,
) -> None:
    pair = pairs_for("dev", ["resume-011__jd-syn-03"])[0]
    async with fake_upstream(_fake_model) as model:
        shared = await _run_pair(session_factory, model, pair, Version.B)
        reloaded = type(shared).model_validate_json(shared.model_dump_json())
        checked = [
            await _run_pair(session_factory, model, pair, version, reloaded)
            for version in (Version.C, Version.D)
        ]

    assert reloaded.shared_draft is not None
    assert reloaded.shared_draft.replay.match_items
    assert reloaded.shared_draft.replay.plan
    for result in checked:
        assert result.status is RunStatus.SUCCEEDED
        assert result.shared_calls == shared.calls
        assert not {"match", "rewrite"} & {
            call["purpose"]
            for call in result.calls
            if call["prompt_version"] != "revise_bullet@1"
        }
        assert [(row.experience, row.fact_ids) for row in result.bullets] == [
            (row.experience, row.fact_ids) for row in shared.bullets
        ]
    assert any(call["purpose"] == "ground_check" for call in checked[1].calls)
