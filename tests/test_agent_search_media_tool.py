from app.agent.tools.impl.search_media import SearchMediaTool


def test_tool_message_displays_special_season_zero():
    """媒体搜索提示应展示显式季 0。"""
    tool = SearchMediaTool(session_id="session-1", user_id="10001")

    message = tool.get_tool_message(title="测试剧", media_type="tv", season=0)

    assert "第0季" in message
