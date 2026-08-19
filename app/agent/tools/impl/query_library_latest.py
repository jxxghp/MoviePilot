"""查询媒体服务器最近入库条目工具"""

import asyncio
import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.orchestration.mediaserver import MediaServerChain
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.log import logger

PAGE_SIZE = 20


class QueryLibraryLatestInput(BaseModel):
    """查询媒体服务器最近入库条目工具的输入参数模型"""

    server: Optional[str] = Field(
        None,
        description="Media server name (optional, if not specified queries all enabled media servers)",
    )
    page: Optional[int] = Field(
        1, description="Page number for pagination (default: 1, 20 items per page)"
    )


class QueryLibraryLatestTool(MoviePilotTool):
    """查询媒体服务器最近入库的影视或音乐条目。"""

    name: str = "query_library_latest"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Library,
        ToolTag.Media,
    ]
    description: str = (
        "Query the latest media items added to configured media servers. Returns any server-supported movies, "
        "TV, recordings, or albums with titles, images, links, and metadata. Supports 20-item pagination."
    )
    args_schema: Type[BaseModel] = QueryLibraryLatestInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        server = kwargs.get("server")
        page = kwargs.get("page", 1)

        parts = ["查询媒体服务器最近入库条目"]

        if server:
            parts.append(f"服务器: {server}")
        else:
            parts.append("所有服务器")

        parts.append(f"第{page}页")

        return " | ".join(parts)

    @staticmethod
    def _get_enabled_servers() -> list[str]:
        """同步读取启用的媒体服务器列表。"""
        mediaservers = ServiceConfigHelper.get_mediaserver_configs()
        return [ms.name for ms in mediaservers if ms.enabled]

    @staticmethod
    def _load_latest_items(
        server_name: str, count: int, username: Optional[str] = None
    ) -> list[dict]:
        """
        媒体服务器 SDK 和 requests 调用都是同步的，这里在线程池中转换为可序列化结果。
        """
        latest_items = MediaServerChain().latest(
            server=server_name, count=count, username=username
        )
        if not latest_items:
            return []
        return [
            {
                **item.model_dump(exclude_none=True),
                "server": server_name,
            }
            for item in latest_items
        ]

    async def run(
        self, server: Optional[str] = None, page: Optional[int] = 1, **kwargs
    ) -> str:
        """并行读取媒体服务器最近入库结果并执行统一分页。"""
        page = max(1, page or 1)
        # 为了支持分页，需要获取足够多的数据再切片
        fetch_count = page * PAGE_SIZE
        logger.info(f"执行工具: {self.name}, 参数: server={server}, page={page}")
        try:
            # 如果没有指定服务器，获取所有启用的媒体服务器
            if not server:
                enabled_servers = self._get_enabled_servers()
                if not enabled_servers:
                    return "未找到启用的媒体服务器"
                server_results = await asyncio.gather(
                    *[
                        self.run_blocking(
                            "mediaserver",
                            self._load_latest_items,
                            server_name,
                            fetch_count,
                            self._username,
                        )
                        for server_name in enabled_servers
                    ]
                )
                results = [
                    item for items in server_results for item in items if items
                ]
            else:
                results = await self.run_blocking(
                    "mediaserver",
                    self._load_latest_items,
                    server,
                    fetch_count,
                    self._username,
                )

            if not results:
                server_info = f"服务器 {server}" if server else "所有服务器"
                return f"未找到 {server_info} 的最近入库条目"

            # 分页
            total_count = len(results)
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_results = results[start:end]

            if not page_results:
                total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
                return f"第 {page} 页没有数据，共 {total_count} 条结果，共 {total_pages} 页。"

            total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
            payload_msg = f"第 {page}/{total_pages} 页，当前页 {len(page_results)} 条结果，共 {total_count} 条。"
            if page < total_pages:
                payload_msg += f" 可使用 page={page + 1} 获取下一页。"

            result_json = json.dumps(page_results, ensure_ascii=False, indent=2)
            return f"{payload_msg}\n\n{result_json}"

        except Exception as e:
            logger.error(f"查询媒体服务器最近入库影片失败: {e}", exc_info=True)
            return f"查询媒体服务器最近入库影片时发生错误: {str(e)}"
