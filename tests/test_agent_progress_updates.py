from unittest.mock import patch

from app.agent.prompt import prompt_manager
from app.core.config import settings
from app.schemas.types import MessageChannel


def test_non_verbose_prompt_keeps_long_running_tasks_user_visible() -> None:
    """非详细模式下的长任务仍应主动向用户提供阶段性进度。"""
    with patch.object(settings, "AI_AGENT_VERBOSE", False):
        prompt = prompt_manager.get_agent_prompt(
            channel=MessageChannel.WebAgent.value
        )

    assert "before the first tool call" in prompt
    assert "after a meaningful milestone or several tool calls" in prompt
    assert "approximately every 30 to 60 seconds whenever you regain control" in prompt
    assert "one or two short sentences" in prompt
    assert "do not repeat an unchanged status" in prompt
    assert "Continue working after each update" in prompt
    assert "The final reply must be self-contained" in prompt
    assert "remain completely silent" not in prompt
    assert "DO NOT output any intermediate content" not in prompt
