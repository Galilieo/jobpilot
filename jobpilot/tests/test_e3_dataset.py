"""自检 E3 和 E5 共用的合成岗位、固定 ID 及按简历划分的样本。"""

import json

import pytest
from evals.runners.e1_extract_facts import DEV_SAMPLES, TEST_SAMPLES
from evals.runners.e4_ground_check import load_fact_set

from jobpilot.config import REPO_ROOT
from jobpilot.services.jobs import BasicFields, RequirementType

DATASETS = REPO_ROOT / "evals" / "datasets"
SPLIT = json.loads((DATASETS / "e3-split.json").read_text(encoding="utf-8"))


def test_e3_uses_disjoint_annotated_e1_resumes() -> None:
    """同一份简历的五个配对只能出现在一个集合里。"""
    dev, test = set(SPLIT["dev"]), set(SPLIT["test"])
    assert len(dev) == len(test) == len(SPLIT["dev"]) == len(SPLIT["test"]) == 5
    assert not dev & test
    assert dev <= set(DEV_SAMPLES)
    assert test <= set(TEST_SAMPLES)
    for resume_id in dev | test:
        assert (DATASETS / "resumes" / f"{resume_id}.txt").is_file()
        assert load_fact_set(resume_id), resume_id


def test_e3_uses_five_distinct_jds() -> None:
    assert len(SPLIT["jd_ids"]) == len(set(SPLIT["jd_ids"])) == 5


@pytest.mark.parametrize("jd_id", SPLIT["jd_ids"])
def test_synthetic_jd_requirements_have_fixed_ids_and_quotes(jd_id: str) -> None:
    raw = (DATASETS / "jds" / f"{jd_id}.txt").read_text(encoding="utf-8")
    path = DATASETS / "annotations" / f"{jd_id}.json"
    annotation = json.loads(path.read_text(encoding="utf-8"))
    assert annotation["sample_id"] == jd_id
    assert annotation["source_url"] is None
    fields = BasicFields.model_validate(annotation["basic_fields"])
    assert fields.title in raw
    requirements = annotation["requirements"]
    assert 6 <= len(requirements) <= 12
    assert {RequirementType(row["type"]) for row in requirements} == {
        RequirementType.HARD,
        RequirementType.PREFERRED,
    }
    assert {row["requirement_id"] for row in requirements} == {
        f"{jd_id}-r{index:02d}" for index in range(1, len(requirements) + 1)
    }
    for requirement in requirements:
        assert (
            requirement["source_quote"] and raw.count(requirement["source_quote"]) == 1
        )
