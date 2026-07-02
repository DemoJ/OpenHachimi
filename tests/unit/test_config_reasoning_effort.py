# pyrefly: ignore [missing-import]
"""llm.reasoning_effort 配置解析与 WebUI 字段注册测试。

覆盖:
- _config_literal 对 6 个合法档位(none/minimal/low/medium/high/xhigh)的解析;
- 非法值在加载期抛 ValueError(与 supports_vision/detail 等枚举字段校验风格一致);
- 默认值为 none(旧 config.yaml 无此字段时向后兼容);
- AI_MODEL_FIELDS 注册了该 select 字段且选项齐全,保证 WebUI 设置页可调。
"""

import pytest

from openhachimi_agent.core.config._helpers import _config_literal
from openhachimi_agent.core.config.webui_fields import AI_MODEL_FIELDS

# 对齐 openai SDK 官方 ReasoningEffort 枚举;none 为默认(不思考)。
_VALID_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]


@pytest.mark.parametrize("effort", _VALID_EFFORTS)
def test_reasoning_effort_parses_each_valid_value(effort):
    """6 个合法档位都能从 yaml 原样解析。"""
    llm_config = {"reasoning_effort": effort}
    assert _config_literal(llm_config, "reasoning_effort", set(_VALID_EFFORTS), "none") == effort


def test_reasoning_effort_defaults_to_none_when_missing():
    """旧 config.yaml 未配置 reasoning_effort 时回退默认 none,向后兼容。"""
    assert _config_literal({}, "reasoning_effort", set(_VALID_EFFORTS), "none") == "none"


def test_reasoning_effort_rejects_invalid_value():
    """非法档位在加载期抛 ValueError,提示合法取值。"""
    llm_config = {"reasoning_effort": "ultra"}
    with pytest.raises(ValueError, match="reasoning_effort"):
        _config_literal(llm_config, "reasoning_effort", set(_VALID_EFFORTS), "none")


def test_reasoning_effort_is_case_insensitive():
    """大写/混合大小写按现有 _config_literal 语义归一化(supports_vision 等同样如此)。"""
    assert _config_literal(
        {"reasoning_effort": "HIGH"}, "reasoning_effort", set(_VALID_EFFORTS), "none"
    ) == "high"


def test_webui_registers_reasoning_effort_select_with_all_options():
    """WebUI「思考深度」下拉已注册且 6 个选项齐全。"""
    field = next(
        (f for f in AI_MODEL_FIELDS if f.get("path") == "llm.reasoning_effort"),
        None,
    )
    assert field is not None, "llm.reasoning_effort 未在 AI_MODEL_FIELDS 注册"
    assert field["kind"] == "select"
    assert field["group"] == "llm"
    assert field["options"] == _VALID_EFFORTS
