"""
验证网络搜索工具的网站筛选、搜索回退与结果约束。
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.search_web import (
    DDGS_AUTO_BACKEND,
    DEFAULT_SEARCH_ENGINE,
    SearchWebTool,
)
from app.runtime.config import settings


def test_build_search_query_adds_site_filter() -> None:
    """
    指定网址时应生成搜索引擎可识别的 site 查询
    """
    site_filter = SearchWebTool._normalize_site_filter("https://docs.python.org/3/")

    assert "docs.python.org" == site_filter.domain
    assert "/3" == site_filter.path
    assert "docs.python.org/3" == site_filter.search_target
    assert "asyncio site:docs.python.org/3" == SearchWebTool._build_search_query("asyncio", site_filter)


def test_build_search_query_keeps_existing_site_operator() -> None:
    """
    用户已写 site 条件时不应重复追加限定条件
    """
    site_filter = SearchWebTool._normalize_site_filter("python.org")

    assert "asyncio site:docs.python.org" == SearchWebTool._build_search_query(
        "asyncio site:docs.python.org", site_filter
    )


def test_filter_results_by_site_matches_domain_and_path() -> None:
    """
    站点过滤应同时约束域名和路径前缀
    """
    site_filter = SearchWebTool._normalize_site_filter("https://docs.python.org/3/")
    results = [
        {"url": "https://docs.python.org/3/library/asyncio.html"},
        {"url": "https://www.docs.python.org/3/tutorial/index.html"},
        {"url": "https://docs.python.org/2/library/asyncio.html"},
        {"url": "https://example.com/3/library/asyncio.html"},
    ]

    filtered_results = SearchWebTool._filter_results_by_site(results, site_filter)

    assert 2 == len(filtered_results)
    assert "https://docs.python.org/3/library/asyncio.html" == filtered_results[0]["url"]


def test_auto_search_plan_falls_back_to_search_engine() -> None:
    """
    自动模式应只使用 DDGS 搜索引擎后端
    """
    search_plan = SearchWebTool._get_search_plan(DEFAULT_SEARCH_ENGINE)

    assert [DEFAULT_SEARCH_ENGINE] == search_plan


def test_auto_ddgs_backend_excludes_bing() -> None:
    """
    DDGS 自动搜索后端不应包含 Bing
    """
    auto_backends = SearchWebTool._get_ddgs_backend(DEFAULT_SEARCH_ENGINE).split(",")

    assert "bing" not in auto_backends
    assert "duckduckgo" in auto_backends
    assert DDGS_AUTO_BACKEND == ",".join(auto_backends)


def test_bing_search_engine_is_not_supported() -> None:
    """
    Bing 不应再作为可选 DDGS 搜索后端暴露
    """
    tool = SearchWebTool(session_id="session-1", user_id="10001")

    result = asyncio.run(tool.run(query="asyncio", search_engine="bing"))

    assert "不支持的搜索源 'bing'" in result


def test_ddgs_alias_uses_auto_backend() -> None:
    """
    DDGS 别名应映射到自动 DDGS 后端
    """
    assert DEFAULT_SEARCH_ENGINE == SearchWebTool._normalize_search_engine("ddgs")


def test_run_uses_specific_search_engine_and_site_filter() -> None:
    """
    显式搜索引擎和指定网址应传入后端搜索调用
    """

    async def _run_tool():
        """
        执行一次带 mock 后端的搜索工具调用
        """
        tool = SearchWebTool(session_id="session-1", user_id="10001")
        with patch.object(
            tool,
            "_search_with_backend",
            new_callable=AsyncMock,
        ) as search_mock:
            search_mock.return_value = [
                {
                    "title": "asyncio",
                    "snippet": "Python asyncio docs",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "source": "DuckDuckGo",
                }
            ]

            result = await tool.run(
                query="asyncio",
                max_results=5,
                search_engine="duckduckgo",
                site_url="https://docs.python.org/3/",
            )
            return result, search_mock.await_args.kwargs

    result, call_kwargs = asyncio.run(_run_tool())
    payload = json.loads(result)

    assert "duckduckgo" == call_kwargs["engine"]
    assert "asyncio site:docs.python.org/3" == call_kwargs["query"]
    assert "docs.python.org" == call_kwargs["site_filter"].domain
    assert 1 == payload["total_results"]
    assert "DuckDuckGo" == payload["results"][0]["source"]


def test_ddgs_uses_system_proxy_by_default() -> None:
    """
    DDGS 搜索默认应使用系统代理配置
    """

    async def _run_tool():
        """
        执行一次带 mock DDGS 后端的搜索工具调用
        """
        tool = SearchWebTool(session_id="session-1", user_id="10001")
        with (
            patch.object(settings, "PROXY_HOST", "http://proxy.example.com:7890"),
            patch("app.agent.tools.impl.search_web.DDGS") as ddgs_mock,
        ):
            ddgs = ddgs_mock.return_value.__enter__.return_value
            ddgs.text.return_value = [
                {
                    "title": "asyncio",
                    "body": "Python asyncio docs",
                    "href": "https://docs.python.org/3/library/asyncio.html",
                }
            ]

            results = await tool._search_ddgs(
                query="asyncio",
                max_results=1,
                search_engine="duckduckgo",
            )
            return results, ddgs_mock.call_args.kwargs

    results, ddgs_kwargs = asyncio.run(_run_tool())

    assert "http://proxy.example.com:7890" == ddgs_kwargs["proxy"]
    assert 1 == len(results)
