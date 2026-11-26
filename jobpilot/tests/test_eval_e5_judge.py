"""验证裁判的覆盖校验和二分类一致性计分。"""

import pytest
from evals.runners.e5_dataset import pairs_for
from evals.runners.e5_judge import Candidate, JudgeBatch
from evals.runners.e5_run import EvalBullet, Version
from evals.runners.e5_score import agreement
from pydantic import ValidationError

from jobpilot.tests.test_call_accounting import completion_response, fake_upstream


async def test_judge_does_not_silently_lose_a_candidate() -> None:
    pair = pairs_for("dev")[0]
    batch = JudgeBatch(
        Version.D,
        pair,
        tuple(
            Candidate(Version.D, pair, index, EvalBullet(experience=None, text="样句"))
            for index in (1, 2)
        ),
    )
    reply = '{"verdicts":[{"id":1,"types":[],"reason":"有依据"}]}'
    async with fake_upstream(lambda _: completion_response(reply)) as client:
        completion = await client.chat.completions.create(model="fake", messages=[])

    with pytest.raises(ValidationError, match="每个候选 id 必须恰好判一次"):
        batch.parse(completion)


def test_agreement_corrects_for_chance() -> None:
    observed, kappa = agreement([True, True, False, False], [True, False, False, False])
    assert observed == 0.75
    assert kappa == 0.5
