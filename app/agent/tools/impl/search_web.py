import asyncio
import json
import random
import re
from typing import Optional, Type, List, Dict

import httpx
from ddgs import DDGS
from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.core.config import settings
from app.log import logger

# 搜索超时时间（秒）
SEARCH_TIMEOUT = 20


class SearchWebInput(BaseModel):
    """搜索网络内容工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    query: str = Field(
        ..., description="The search query string to search for on the web"
    )
    max_results: Optional[int] = Field(
        20,
        description="Maximum number of search results to return (default: 20, max: 20)",
    )


class SearchWebTool(MoviePilotTool):
    name: str = "search_web"
    description: str = "Search the web for information when you need to find current information, facts, or references that you're uncertain about. Returns search results with titles, snippets, and URLs. Use this tool to get up-to-date information from the internet."
    args_schema: Type[BaseModel] = SearchWebInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 20)
        return f"搜索网络内容: {query} (最多返回 {max_results} 条结果)"

    async def run(self, query: str, max_results: Optional[int] = 20, **kwargs) -> str:
        """
        执行网络搜索
        """
        logger.info(
            f"执行工具: {self.name}, 参数: query={query}, max_results={max_results}"
        )

        try:
            # 限制最大结果数
            max_results = min(max(1, max_results or 20), 20)
            results = []

            # 1. 优先使用 Exa (如果配置了 API Key)
            if settings.EXA_API_KEY:
                logger.info("使用 Exa 进行搜索...")
                results = await self._search_exa(query, max_results)

            # 2. 如果没有结果或未配置 Exa，使用 Tavily (如果配置了 API Key)
            if not results and settings.TAVILY_API_KEY:
                logger.info("使用 Tavily 进行搜索...")
                results = await self._search_tavily(query, max_results)

            # 3. 如果没有结果或未配置 Tavily，使用 DuckDuckGo
            if not results:
                logger.info("使用 DuckDuckGo 进行搜索...")
                results = await self._search_duckduckgo(query, max_results)

            if not results:
                return f"未找到与 '{query}' 相关的搜索结果"

            # 格式化并裁剪结果
            formatted_results = self._format_and_truncate_results(results, max_results)
            return json.dumps(formatted_results, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"搜索网络内容失败: {str(e)}"
            logger.error(f"搜索网络内容失败: {e}", exc_info=True)
            return error_message

    @staticmethod
    async def _search_tavily(query: str, max_results: int) -> List[Dict]:
        """使用 Tavily API 进行搜索"""
        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
                # 从设置中随机选择一个 API Key（如果有多个）
                tavity_api_key = random.choice(settings.TAVILY_API_KEY)
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavity_api_key,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": max_results,
                        "include_answer": False,
                        "include_images": False,
                        "include_raw_content": False,
                    },
                )
                response.raise_for_status()
                data = response.json()

                results = []
                for result in data.get("results", []):
                    results.append(
                        {
                            "title": result.get("title", ""),
                            "snippet": result.get("content", ""),
                            "url": result.get("url", ""),
                            "source": "Tavily",
                        }
                    )
                return results
        except Exception as e:
            logger.warning(f"Tavily 搜索失败: {e}")
            return []

    @staticmethod
    async def _search_exa(query: str, max_results: int) -> List[Dict]:
        """使用 Exa API 进行搜索"""
        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
                response = await client.post(
                    "https://api.exa.ai/search",
                    headers={
                        "x-api-key": settings.EXA_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "numResults": max_results,
                        "type": "auto",
                        "contents": {"highlights": {"maxCharacters": 2000}},
                    },
                )
                response.raise_for_status()
                data = response.json()

                results = []
                for result in data.get("results", []):
                    highlights = result.get("highlights", [])
                    snippet = (
                        highlights[0] if highlights else result.get("text", "")[:500]
                    )
                    results.append(
                        {
                            "title": result.get("title", ""),
                            "snippet": snippet,
                            "url": result.get("url", ""),
                            "source": "Exa",
                        }
                    )
                return results
        except Exception as e:
            logger.warning(f"Exa 搜索失败: {e}")
            return []

    @staticmethod
    def _get_proxy_url(proxy_setting) -> Optional[str]:
        """从代理设置中提取代理URL"""
        if not proxy_setting:
            return None
        if isinstance(proxy_setting, dict):
            return proxy_setting.get("http") or proxy_setting.get("https")
        return proxy_setting

    async def _search_duckduckgo(self, query: str, max_results: int) -> List[Dict]:
        """使用 duckduckgo-search (DDGS) 进行搜索"""
        try:

            def sync_search():
                results = []
                ddgs_kwargs = {"timeout": SEARCH_TIMEOUT}
                proxy_url = self._get_proxy_url(settings.PROXY)
                if proxy_url:
                    ddgs_kwargs["proxy"] = proxy_url

                try:
                    with DDGS(**ddgs_kwargs) as ddgs:
                        ddgs_gen = ddgs.text(query, max_results=max_results)
                        if ddgs_gen:
                            for result in ddgs_gen:
                                results.append(
                                    {
                                        "title": result.get("title", ""),
                                        "snippet": result.get("body", ""),
                                        "url": result.get("href", ""),
                                        "source": "DuckDuckGo",
                                    }
                                )
                except Exception as err:
                    logger.warning(f"DuckDuckGo search process failed: {err}")
                return results

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, sync_search)

        except Exception as e:
            logger.warning(f"DuckDuckGo 搜索失败: {e}")
            return []

    @staticmethod
    def _format_and_truncate_results(results: List[Dict], max_results: int) -> Dict:
        """格式化并裁剪搜索结果"""
        formatted = {"total_results": len(results), "results": []}

        for idx, result in enumerate(results[:max_results], 1):
            title = result.get("title", "")[:200]
            snippet = result.get("snippet", "")
            url = result.get("url", "")
            source = result.get("source", "Unknown")

            # 裁剪摘要
            max_snippet_length = 1000  # 增加到1000字符，提供更多上下文
            if len(snippet) > max_snippet_length:
                snippet = snippet[:max_snippet_length] + "..."

            # 清理文本
            snippet = re.sub(r"\s+", " ", snippet).strip()

            formatted["results"].append(
                {
                    "rank": idx,
                    "title": title,
                    "snippet": snippet,
                    "url": url,
                    "source": source,
                }
            )

        if len(results) > max_results:
            formatted["note"] = f"仅显示前 {max_results} 条结果。"

        return formatted
