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


def test_both_evidence_bases_use_the_same_hidden_candidates() -> None:
    from evals.runners.e5_judge import EvidenceBasis, _messages
    from evals.runners.e5_run import eval_prompt
    from evals.runners.e5_validation import candidates

    samples = candidates()
    assert len(samples) == 40
    first = samples[0]
    batch = JudgeBatch(first.version, first.pair, (first,))
    prompt = eval_prompt("e5_judge")
    assert prompt.version == 2
    [confirmed, resume] = [
        _messages(batch, prompt, basis)[1]["content"] for basis in EvidenceBasis
    ]
    assert '"facts":' in confirmed and '"facts":' not in resume
    assert first.pair.resume_text in resume and first.pair.resume_text not in confirmed
    assert (
        confirmed.split('label="candidates"')[1]
        == resume.split('label="candidates"')[1]
    )
    assert first.key not in confirmed and first.key not in resume


def test_independent_draw_does_not_include_either_pilot_pair() -> None:
    from collections import Counter

    from evals.runners.e5_validation import (
        PILOT_PAIRS,
        candidates,
        draw_from_development,
    )

    pool = candidates()
    pilot = next(pair for pair in pairs_for("dev") if pair.key in PILOT_PAIRS)
    pool.extend(
        Candidate(item.version, pilot, item.index, item.bullet) for item in list(pool)
    )
    result = draw_from_development(pool)
    assert len(result) == 40
    assert Counter(item.version for item in result) == dict.fromkeys(Version, 8)
    assert not {item.pair.key for item in result} & PILOT_PAIRS


def test_public_manifest_matches_the_synthetic_inputs() -> None:
    from json import loads

    from evals.runners.e5_dataset import dataset_paths, dataset_version

    from jobpilot.config import REPO_ROOT

    manifest_path = REPO_ROOT / "evals" / "benchmark" / "e5" / "manifest.json"
    manifest = loads(manifest_path.read_text(encoding="utf-8"))
    for split in ("dev", "test"):
        assert manifest[split]["dataset"] == dataset_version(split)
        assert manifest[split]["inputs"] == [
            str(path.relative_to(REPO_ROOT)) for path in dataset_paths(split)
        ]
