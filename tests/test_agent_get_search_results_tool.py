import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.tools.impl._torrent_search_utils import simplify_search_result
from app.agent.tools.impl.get_search_results import GetSearchResultsTool
from app.core.context import Context, TorrentInfo


def _build_context(
    title: str,
    *,
    description: str = None,
    labels: list = None,
    index: int = 1,
) -> Context:
    """构造种子搜索结果上下文。"""
    return Context(
        torrent_info=TorrentInfo(
            title=title,
            description=description,
            labels=labels or [],
            enclosure=f"https://example.com/download/{index}",
            size=1024,
            seeders=index,
            site_name="测试站点",
        )
    )


def _run_tool(items: list[Context], **kwargs) -> str:
    """使用指定缓存结果运行搜索结果工具。"""
    search_chain = MagicMock()
    search_chain.async_last_search_results = AsyncMock(return_value=items)
    with patch(
        "app.agent.tools.impl.get_search_results.SearchChain",
        return_value=search_chain,
    ):
        return asyncio.run(
            GetSearchResultsTool(session_id="session-1", user_id="10001").run(
                **kwargs
            )
        )


def test_simplify_search_result_only_includes_description_when_requested():
    """精简结果应按参数控制简介输出，避免默认增加上下文长度。"""
    context = _build_context("Movie.2026.1080p", description="简繁特效字幕")

    default_result = simplify_search_result(context, 1)
    detailed_result = simplify_search_result(context, 1, include_description=True)

    assert "description" not in default_result["torrent_info"]
    assert detailed_result["torrent_info"]["description"] == "简繁特效字幕"


def test_content_pattern_matches_title_description_and_labels():
    """内容正则应联合匹配标题、简介和标签，并可返回命中的简介。"""
    items = [
        _build_context("Movie.Special.Effect.2026", description="普通字幕", index=1),
        _build_context("Movie.2026.1080p", description="简繁特效字幕", index=2),
        _build_context("Movie.2026.2160p", description="国语音轨", labels=["官译"], index=3),
        _build_context("Movie.2026.WEB-DL", description="英文字幕", index=4),
    ]

    result = _run_tool(
        items,
        content_pattern="Special.Effect|特效字幕|官译",
        include_description=True,
    )
    payload = json.loads(result)

    assert payload["total_count"] == 3
    assert [item["torrent_info"]["description"] for item in payload["results"]] == [
        "普通字幕",
        "简繁特效字幕",
        "国语音轨",
    ]


def test_title_pattern_keeps_title_only_matching_semantics():
    """标题正则不应因新增内容筛选而匹配简介或标签。"""
    items = [
        _build_context("Movie.特效.2026", description="普通字幕", index=1),
        _build_context("Movie.2026.1080p", description="简繁特效字幕", index=2),
        _build_context("Movie.2026.2160p", labels=["特效"], index=3),
    ]

    result = _run_tool(items, title_pattern="特效", include_description=True)
    payload = json.loads(result)

    assert payload["total_count"] == 1
    assert payload["results"][0]["torrent_info"]["title"] == "Movie.特效.2026"


def test_invalid_content_pattern_returns_validation_message():
    """非法内容正则应返回明确错误，不进入搜索结果筛选。"""
    result = _run_tool([_build_context("Movie.2026")], content_pattern="[")

    assert result.startswith("正则表达式格式错误:")
