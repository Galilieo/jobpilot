"""固定模型档案、窗口估算和增量复用的边界。"""

import pytest
from pydantic import ValidationError

from jobpilot.config import ModelPurpose, Settings, settings
from jobpilot.services.request_window import InputTokenHistory, estimate_request_tokens


def test_window_cap_and_agent_budget_are_validated() -> None:
    config = settings.model_dump()
    config["agent_limits"].update(
        context_window_ratio=0.5, context_window_cap_tokens=32_000
    )
    local = Settings.model_validate(config, by_name=True)
    assert local.effective_window(ModelPurpose.AGENT) == 32_000

    config["agent_limits"].update(
        context_window_cap_tokens=None, model_tokens=2_097_152
    )
    production = Settings.model_validate(config, by_name=True)
    assert production.effective_window(ModelPurpose.AGENT) == 524_288

    config["agent_limits"]["model_tokens"] -= 1
    with pytest.raises(ValidationError, match="有效窗口"):
        Settings.model_validate(config, by_name=True)


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
