"""依据检查第三层：假上游返回预设输出，网关、校验和调用记录都真实执行。"""

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.models import AiRun, FactSource, Responsibility, User
from jobpilot.services.grounding import GroundingFact, IssueKind
from jobpilot.services.llm_gateway import BudgetExhaustedError, ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.semantic_check import (
    SemanticOutput,
    SemanticVerdict,
    ground_check,
)
from jobpilot.tests.test_call_accounting import (
    calls_of,
    completion_response,
    empty_choices_response,
    fake_upstream,
)

Factory = async_sessionmaker[AsyncSession]

SENTENCE = "为运单查询接口加入失败重试，显著提升系统稳定性"
FACT = GroundingFact(
    fact_id=uuid4(),
    experience="云栈物流科技 后端开发实习",
    responsibility=Responsibility.OWN,
    text="给运单查询接口加了失败重试",
    source=FactSource.RESUME,
    quote_verified=True,
)
SUPPORTED = {"verdict": "supported", "unsupported_spans": []}
PARTIAL = {
    "verdict": "partial",
    "unsupported_spans": [{"text": "显著提升系统稳定性", "reason": "事实没提稳定性"}],
}


@pytest.fixture
async def run(session_factory: Factory) -> AiRun:
    async with session_factory.begin() as session:
        owner = User(email="ground@example.invalid", hashed_password="test")
        session.add(owner)
        await session.flush()
        run = AiRun(user_id=owner.user_id, kind="tailor")
        session.add(run)
    return run


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_retry_delay_seconds", 0)


def budget() -> ModelBudget:
    return ModelBudget(remaining_calls=5, remaining_tokens=20_000)


async def check_with(session_factory: Factory, run: AiRun, *responses: httpx.Response):
    pending = list(responses)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return pending.pop(0)

    async with fake_upstream(respond) as client:
        result = await ground_check(
            session_factory,
            run.user_id,
            run.run_id,
            client=client,
            budget=budget(),
            sentence=SENTENCE,
            facts=[FACT],
        )
    return result, requests


def output(value: dict) -> httpx.Response:
    return completion_response(json.dumps(value, ensure_ascii=False))


async def test_supported_sentence_passes(session_factory: Factory, run: AiRun) -> None:
    result, requests = await check_with(session_factory, run, output(SUPPORTED))

    assert result.passed and result.verdict is SemanticVerdict.SUPPORTED
    [call] = await calls_of(session_factory, run)
    assert call.purpose == "ground_check"
    assert call.prompt_version == "ground_check@1"


async def test_request_carries_only_sentence_and_fact_text(
    session_factory: Factory, run: AiRun
) -> None:
    _, [request] = await check_with(session_factory, run, output(SUPPORTED))
    body = json.loads(request.content)
    assert body["temperature"] == 0
    user = body["messages"][1]["content"]
    assert '<untrusted_data label="sentence">' in user
    assert '<untrusted_data label="facts">' in user
    assert FACT.text in user and FACT.experience in user
    # 不给职责字段、来源和事实 ID，也没有岗位信息。
    for absent in ("responsibility", "source", str(FACT.fact_id), "job"):
        assert absent not in user


async def test_partial_verdict_reports_the_unsupported_span(
    session_factory: Factory, run: AiRun
) -> None:
    result, _ = await check_with(session_factory, run, output(PARTIAL))

    assert result.verdict is SemanticVerdict.PARTIAL
    [issue] = result.issues
    assert issue.kind is IssueKind.SEMANTIC
    assert issue.fragment == "显著提升系统稳定性"
    assert "事实没提稳定性" in issue.message


async def test_span_not_in_sentence_is_dropped_but_verdict_kept(
    session_factory: Factory, run: AiRun
) -> None:
    invented = {
        "verdict": "unsupported",
        "unsupported_spans": [{"text": "大幅提升吞吐量", "reason": "句子里没有这段"}],
    }
    result, _ = await check_with(session_factory, run, output(invented))

    assert result.verdict is SemanticVerdict.UNSUPPORTED
    [issue] = result.issues
    assert issue.kind is IssueKind.SEMANTIC and issue.fragment is None


async def test_inconsistent_output_is_repaired_once(
    session_factory: Factory, run: AiRun
) -> None:
    inconsistent = {
        "verdict": "supported",
        "unsupported_spans": PARTIAL["unsupported_spans"],
    }
    result, requests = await check_with(
        session_factory, run, output(inconsistent), output(PARTIAL)
    )

    assert result.verdict is SemanticVerdict.PARTIAL
    assert len(requests) == 2
    calls = await calls_of(session_factory, run)
    assert {call.prompt_version for call in calls} == {
        "ground_check@1",
        "ground_check@1+repair_json@1",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "supported", "unsupported_spans": PARTIAL["unsupported_spans"]},
        {"verdict": "partial", "unsupported_spans": []},
        {"verdict": "maybe", "unsupported_spans": []},
    ],
)
async def test_output_still_invalid_after_repair_fails_closed(
    session_factory: Factory, run: AiRun, bad: dict
) -> None:
    result, requests = await check_with(session_factory, run, output(bad), output(bad))

    assert result.verdict is None and not result.passed
    assert [issue.kind for issue in result.issues] == [IssueKind.CHECK_UNAVAILABLE]
    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 500])
async def test_upstream_error_fails_closed(
    session_factory: Factory, run: AiRun, status: int
) -> None:
    error = httpx.Response(status, json={"error": {"message": "boom"}})
    responses = [error] * settings.model_max_attempts
    result, _ = await check_with(session_factory, run, *responses)

    assert not result.passed
    assert [issue.kind for issue in result.issues] == [IssueKind.CHECK_UNAVAILABLE]


async def test_budget_exhaustion_is_not_swallowed(
    session_factory: Factory, run: AiRun
) -> None:
    async with fake_upstream(lambda request: output(SUPPORTED)) as client:
        with pytest.raises(BudgetExhaustedError):
            await ground_check(
                session_factory,
                run.user_id,
                run.run_id,
                client=client,
                budget=ModelBudget(remaining_calls=0, remaining_tokens=20_000),
                sentence=SENTENCE,
                facts=[FACT],
            )


def test_prompt_example_follows_program_rules() -> None:
    text = load_prompt("ground_check").text.split("## 示例", 1)[1]
    sentence = text.split('<untrusted_data label="sentence">\n', 1)[1].split("\n", 1)[0]
    example = SemanticOutput.model_validate_json(text.split("输出：\n", 1)[1].strip())
    assert example.verdict is not SemanticVerdict.SUPPORTED
    assert all(span.text in sentence for span in example.unsupported_spans)


async def test_empty_choices_reports_check_unavailable(
    session_factory: Factory, run: AiRun
):
    result, requests = await check_with(session_factory, run, empty_choices_response())
    assert len(requests) == 1
    assert result.verdict is None and not result.passed
    [issue] = result.issues
    assert issue.kind is IssueKind.CHECK_UNAVAILABLE
    assert "EmptyModelResponseError" in issue.message
