from unittest.mock import patch

from app.agent.prompt import prompt_manager
from app.core.config import settings
from app.schemas.types import MessageChannel


def test_progress_prompt_is_independent_from_tool_display_mode() -> None:
    """进度沟通规则不应随工具逐条或汇总展示模式变化。"""
    with patch.object(settings, "AI_AGENT_VERBOSE", False):
        summary_mode_prompt = prompt_manager.get_agent_prompt(
            channel=MessageChannel.WebAgent.value
        )
    with patch.object(settings, "AI_AGENT_VERBOSE", True):
        verbose_mode_prompt = prompt_manager.get_agent_prompt(
            channel=MessageChannel.WebAgent.value
        )

    assert summary_mode_prompt == verbose_mode_prompt
    assert "meaningful changes in understanding or execution" in summary_mode_prompt
    assert "not on elapsed time or the number of tool calls" in summary_mode_prompt
    assert "one or two tools have finished" in summary_mode_prompt
    assert "useful preliminary conclusion" in summary_mode_prompt
    assert "materially changes the working direction" in summary_mode_prompt
    assert "sustained blocker the user should know about" in summary_mode_prompt
    assert "including the key evidence and what you will do next" in summary_mode_prompt
    assert "brevity is not a goal by itself" in summary_mode_prompt
    assert "do not repeat an unchanged status" in summary_mode_prompt.lower()
    assert "Continue working after each update" in summary_mode_prompt
    assert "The final reply must be self-contained" in summary_mode_prompt
    assert "before the first tool call" not in summary_mode_prompt
    assert "approximately every 30 to 60 seconds" not in summary_mode_prompt
    assert "one or two short sentences" not in summary_mode_prompt
    assert "remain completely silent" not in summary_mode_prompt
    assert "DO NOT output any intermediate content" not in summary_mode_prompt
