"""固定模型档案、窗口估算和增量复用的边界。"""

import pytest
from pydantic import ValidationError

from jobpilot.config import AgentLimits, ModelPurpose, Settings, settings
from jobpilot.services.llm_gateway import BudgetExhaustedError, ModelBudget
from jobpilot.services.request_window import InputTokenHistory, estimate_request_tokens


def test_window_cap_is_applied() -> None:
    config = settings.model_dump()
    config["agent_limits"].update(
        context_window_ratio=0.5, context_window_cap_tokens=32_000
    )
    local = Settings.model_validate(config, by_name=True)
    assert local.effective_window(ModelPurpose.AGENT) == 32_000


def test_defaults_use_half_window_and_only_count_limits() -> None:
    config = settings.model_dump()
    config["agent_limits"] = AgentLimits().model_dump()
    defaults = Settings.model_validate(config, by_name=True)
    assert defaults.effective_window(ModelPurpose.AGENT) == 524_288
    assert defaults.agent_limits.model_tokens is None
    assert Settings.model_fields["daily_token_limit"].default is None

    # 每轮 token 上限是固定数，不再要求随窗口放大。
    config["agent_limits"]["model_tokens"] = 1_000
    assert (
        Settings.model_validate(config, by_name=True).agent_limits.model_tokens == 1_000
    )


def test_budget_without_token_limit_only_counts_calls() -> None:
    budget = ModelBudget(remaining_calls=1, remaining_tokens=None)
    budget.reserve(10_000_000)
    with pytest.raises(BudgetExhaustedError, match="次数"):
        budget.reserve(1)

    limited = ModelBudget(remaining_calls=5, remaining_tokens=100)
    with pytest.raises(BudgetExhaustedError, match="token"):
        limited.reserve(101)


def test_config_rejects_unregistered_models() -> None:
    config = settings.model_dump()
    config["agent"]["model_name"] = "unknown-model"
    with pytest.raises(ValidationError, match="unknown-model"):
        Settings.model_validate(config, by_name=True)


def test_only_identical_sent_prefix_can_reuse_usage() -> None:
    first = [{"role": "system", "content": "规则" * 100}]
    next_turn = [*first, {"role": "assistant", "content": "工具结果"}]
    history = InputTokenHistory()
    history.record("settings-a", first, [], used=42)
    incremental = history.estimate("settings-a", next_turn, [])
    assert incremental < estimate_request_tokens(next_turn, [])
    assert history.estimate("settings-b", next_turn, []) == estimate_request_tokens(
        next_turn, []
    )
    changed = [{"role": "system", "content": "改过的规则"}, *next_turn[1:]]
    assert history.estimate("settings-a", changed, []) == estimate_request_tokens(
        changed, []
    )
    history.record("settings-a", next_turn, [], used=None)
    assert history.estimate("settings-a", next_turn, []) == estimate_request_tokens(
        next_turn, []
    )
