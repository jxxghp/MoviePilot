from unittest.mock import Mock

import pytest

from app.agent.llm.helper import LLMHelper


@pytest.mark.parametrize(
    ("provider", "model", "thinking_level", "expected"),
    [
        (
            "openai",
            "deepseek-v4.1-flash",
            "high",
            {"reasoning_effort": "high"},
        ),
        (
            "openai",
            "grok-4.6",
            "max",
            {"reasoning_effort": "xhigh"},
        ),
        (
            "chatgpt",
            "claude-sonnet-4-5",
            "medium",
            {"reasoning_effort": "medium"},
        ),
    ],
)
def test_openai_compatible_models_forward_reasoning_effort(
    provider,
    model,
    thinking_level,
    expected,
):
    """未知模型目录时，OpenAI-compatible 端点应透传用户选择的思考级别。"""
    assert (
        LLMHelper._build_thinking_kwargs(
            provider=provider,
            model=model,
            thinking_level=thinking_level,
        )
        == expected
    )


def test_openai_reasoning_effort_respects_model_catalog():
    """模型目录声明的 effort 范围应约束超出范围的统一思考级别。"""
    metadata = {
        "reasoning": True,
        "reasoning_options": [
            {"type": "effort", "values": ["low", "medium", "high"]}
        ],
    }

    assert LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="catalog-model",
        thinking_level="max",
        model_metadata=metadata,
    ) == {"reasoning_effort": "high"}
    assert LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="catalog-model",
        thinking_level="off",
        model_metadata=metadata,
    ) == {}


def test_openai_reasoning_effort_skips_known_unsupported_model(monkeypatch):
    """模型目录明确不支持思考时应跳过参数并记录原因。"""
    logger = Mock()
    monkeypatch.setattr("app.agent.llm.helper.logger", logger)

    result = LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="chat-model",
        thinking_level="high",
        model_metadata={"reasoning": False, "reasoning_options": None},
    )

    assert result == {}
    logger.warning.assert_called_once()
