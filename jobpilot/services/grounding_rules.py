"""读取 jobpilot/data/grounding_rules.yaml：角色词、程度词和数字单位。

事实拆分的转述检查（services/facts.py）和依据检查（services/grounding.py）共用这一份词表。
"""

import re
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path

import yaml

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import Responsibility

DEFAULT_RULES = REPO_ROOT / "jobpilot" / "data" / "grounding_rules.yaml"

# 职责等级：参与 < 负责 < 主导；na 没有等级。
RESPONSIBILITY_RANK = {
    Responsibility.PARTICIPATE: 0,
    Responsibility.OWN: 1,
    Responsibility.LEAD: 2,
}


class GroundingRulesError(ValueError):
    """词表格式不对，或同一个词出现在两个等级里。"""


@dataclass(frozen=True)
class GroundingRules:
    role_levels: dict[str, Responsibility]
    degree_levels: dict[str, int]
    degree_names: tuple[str, ...]
    unstated_degree_max: int
    number_units: dict[str, str]
    chinese_classifiers: tuple[str, ...]

    @cached_property
    def role_pattern(self) -> re.Pattern[str]:
        return _longest_first(self.role_levels)

    @cached_property
    def degree_pattern(self) -> re.Pattern[str]:
        return _longest_first(self.degree_levels)

    @cached_property
    def units_longest_first(self) -> tuple[str, ...]:
        # 长的先试：「个月」不会被当成「月」。
        return tuple(sorted(self.number_units, key=len, reverse=True))

    def role_rank(self, word: str) -> int:
        return RESPONSIBILITY_RANK[self.role_levels[word]]


def _longest_first(words: dict[str, object]) -> re.Pattern[str]:
    # 正则在同一位置取先列出的分支，按长度从长到短排，「负责人」才不会被当成「负责」。
    return re.compile("|".join(map(re.escape, sorted(words, key=len, reverse=True))))


def load_grounding_rules(path: Path) -> GroundingRules:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    role_levels: dict[str, Responsibility] = {}
    for level, words in (data.get("role_levels") or {}).items():
        responsibility = Responsibility(level)
        if responsibility not in RESPONSIBILITY_RANK:
            raise GroundingRulesError(f"{path.name}：角色等级不能是 {level}")
        for word in words:
            if word in role_levels:
                raise GroundingRulesError(f"{path.name}：角色词「{word}」重复")
            role_levels[word] = responsibility

    degree_levels: dict[str, int] = {}
    names = []
    for rank, entry in enumerate(data.get("degree_levels") or []):
        names.append(entry["name"])
        for word in entry["words"]:
            if word in degree_levels:
                raise GroundingRulesError(f"{path.name}：程度词「{word}」重复")
            degree_levels[word] = rank

    unstated = data.get("unstated_degree_max")
    if unstated not in names:
        raise GroundingRulesError(f"{path.name}：unstated_degree_max 必须是某个等级名")

    if not role_levels or not degree_levels:
        raise GroundingRulesError(f"{path.name}：角色词和程度词都不能为空")

    return GroundingRules(
        role_levels=role_levels,
        degree_levels=degree_levels,
        degree_names=tuple(names),
        unstated_degree_max=names.index(unstated),
        number_units={
            str(k): str(v) for k, v in (data.get("number_units") or {}).items()
        },
        chinese_classifiers=tuple(data.get("chinese_classifiers") or ()),
    )


@cache
def default_rules() -> GroundingRules:
    return load_grounding_rules(DEFAULT_RULES)
