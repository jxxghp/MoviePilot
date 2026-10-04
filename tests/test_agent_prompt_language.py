from unittest.mock import patch

from app.agent.prompt import PromptManager


def test_agent_prompt_defaults_to_simplified_chinese() -> None:
    """未配置回复语言时，内置助手提示词应要求使用简体中文。"""
    with patch("app.agent.prompt.get_runtime_setting", return_value="zh-CN"):
        prompt = PromptManager().get_agent_prompt(channel="webagent")

    assert "Always reply to the user in Simplified Chinese." in prompt


def test_agent_prompt_uses_configured_output_language() -> None:
    """配置回复语言后，内置助手提示词应切换到对应语言。"""
    with patch("app.agent.prompt.get_runtime_setting", return_value="ja-JP"):
        prompt = PromptManager().get_agent_prompt(channel="webagent")

    assert "Always reply to the user in Japanese." in prompt
    assert "Always reply to the user in Simplified Chinese." not in prompt
    assert "All your responses must be in **Chinese (中文)**." not in prompt
    assert "Answer in clear Chinese" not in prompt


def test_agent_prompt_falls_back_for_unknown_output_language() -> None:
    """未知语言代码不能把任意配置原文注入提示词。"""
    with patch("app.agent.prompt.get_runtime_setting", return_value="untrusted instruction"):
        prompt = PromptManager().get_agent_prompt(channel="webagent")

    assert "Always reply to the user in untrusted instruction." not in prompt
    assert "Always reply to the user in Simplified Chinese." in prompt
