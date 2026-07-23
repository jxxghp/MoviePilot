"""获取搜索结果工具"""

import json
import re
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.search import SearchChain
from app.log import logger
from ._torrent_search_utils import (
    TORRENT_RESULT_LIMIT,
    build_filter_options,
    filter_contexts,
    simplify_search_result,
)


class GetSearchResultsInput(BaseModel):
    """获取搜索结果工具的输入参数模型"""

    site: Optional[List[str]] = Field(None, description="Site name filters")
    season: Optional[List[str]] = Field(None, description="Season or episode filters")
    free_state: Optional[List[str]] = Field(None, description="Promotion state filters")
    video_code: Optional[List[str]] = Field(None, description="Video codec filters")
    edition: Optional[List[str]] = Field(None, description="Edition filters")
    resolution: Optional[List[str]] = Field(None, description="Resolution filters")
    release_group: Optional[List[str]] = Field(
        None, description="Release group filters"
    )
    title_pattern: Optional[str] = Field(
        None,
        description="Regular expression pattern to filter torrent titles (e.g., '4K|2160p|UHD', '1080p.*BluRay')",
    )
    content_pattern: Optional[str] = Field(
        None,
        description="Regular expression pattern to filter torrent titles, descriptions, and labels (e.g., '特效字幕|国语|DIY')",
    )
    include_description: Optional[bool] = Field(
        False,
        description="Whether to include torrent descriptions in returned results",
    )
    show_filter_options: Optional[bool] = Field(
        False,
        description="Whether to return only optional filter options for re-checking available conditions",
    )
    page: Optional[int] = Field(
        1,
        description="Page number for pagination (default: 1, each page returns up to 50 results)",
    )


class GetSearchResultsTool(MoviePilotTool):
    """获取并筛选最近一次种子搜索结果"""

    name: str = "get_search_results"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Resource,
    ]
    description: str = "Get cached torrent search results from search_torrents with optional filters. Supports pagination with up to 50 results per page."
    args_schema: Type[BaseModel] = GetSearchResultsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """返回工具执行提示"""
        return "获取搜索结果"

    async def run(
        self,
        site: Optional[List[str]] = None,
        season: Optional[List[str]] = None,
        free_state: Optional[List[str]] = None,
        video_code: Optional[List[str]] = None,
        edition: Optional[List[str]] = None,
        resolution: Optional[List[str]] = None,
        release_group: Optional[List[str]] = None,
        title_pattern: Optional[str] = None,
        content_pattern: Optional[str] = None,
        include_description: bool = False,
        show_filter_options: bool = False,
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        """
        获取并筛选最近一次种子搜索结果

        :param site: 站点名称筛选项
        :param season: 季集筛选项
        :param free_state: 促销状态筛选项
        :param video_code: 视频编码筛选项
        :param edition: 制作版本筛选项
        :param resolution: 分辨率筛选项
        :param release_group: 发布组筛选项
        :param title_pattern: 仅匹配种子标题的正则表达式
        :param content_pattern: 匹配种子标题、简介和标签的正则表达式
        :param include_description: 是否在结果中返回种子简介
        :param show_filter_options: 是否只返回可用筛选项
        :param page: 分页页码
        :param kwargs: 工具框架附加参数
        :return: JSON 格式的搜索结果或错误提示
        """
        page = max(1, page or 1)
        logger.info(
            f"执行工具: {self.name}, 参数: site={site}, season={season}, free_state={free_state}, video_code={video_code}, edition={edition}, resolution={resolution}, release_group={release_group}, title_pattern={title_pattern}, content_pattern={content_pattern}, include_description={include_description}, show_filter_options={show_filter_options}, page={page}"
        )

        try:
            items = await SearchChain().async_last_search_results() or []
            if not items:
                return "没有可用的搜索结果，请先使用 search_torrents 搜索"

            if show_filter_options:
                payload = {
                    "total_count": len(items),
                    "filter_options": build_filter_options(items),
                }
                return json.dumps(payload, ensure_ascii=False, indent=2)

            title_regex_pattern = None
            if title_pattern:
                try:
                    title_regex_pattern = re.compile(title_pattern, re.IGNORECASE)
                except re.error as e:
                    logger.warning(f"正则表达式编译失败: {title_pattern}, 错误: {e}")
                    return f"正则表达式格式错误: {str(e)}"

            content_regex_pattern = None
            if content_pattern:
                try:
                    content_regex_pattern = re.compile(content_pattern, re.IGNORECASE)
                except re.error as e:
                    logger.warning(f"正则表达式编译失败: {content_pattern}, 错误: {e}")
                    return f"正则表达式格式错误: {str(e)}"

            filtered_items = filter_contexts(
                items=items,
                site=site,
                season=season,
                free_state=free_state,
                video_code=video_code,
                edition=edition,
                resolution=resolution,
                release_group=release_group,
            )
            if title_regex_pattern:
                filtered_items = [
                    item
                    for item in filtered_items
                    if item.torrent_info
                    and item.torrent_info.title
                    and title_regex_pattern.search(item.torrent_info.title)
                ]
            if content_regex_pattern:
                content_filtered_items = []
                for item in filtered_items:
                    torrent_info = item.torrent_info
                    if not torrent_info:
                        continue
                    content_values = [torrent_info.title, torrent_info.description]
                    content_values.extend(torrent_info.labels or [])
                    if any(
                        content_regex_pattern.search(str(value))
                        for value in content_values
                        if value
                    ):
                        content_filtered_items.append(item)
                filtered_items = content_filtered_items
            if not filtered_items:
                return "没有符合筛选条件的搜索结果，请调整筛选条件"

            total_count = len(filtered_items)
            filtered_ids = {id(item) for item in filtered_items}
            matched_indices = [
                index
                for index, item in enumerate(items, start=1)
                if id(item) in filtered_ids
            ]

            # 分页
            page_size = TORRENT_RESULT_LIMIT
            start = (page - 1) * page_size
            end = start + page_size
            page_items = filtered_items[start:end]
            page_indices = matched_indices[start:end]

            if not page_items:
                return f"第 {page} 页没有数据，共 {total_count} 条结果，共 {(total_count + page_size - 1) // page_size} 页。"

            results = [
                simplify_search_result(
                    item,
                    index,
                    include_description=include_description,
                )
                for item, index in zip(page_items, page_indices)
            ]
            total_pages = (total_count + page_size - 1) // page_size
            payload = {
                "total_count": total_count,
                "page": page,
                "total_pages": total_pages,
                "results": results,
            }
            if page < total_pages:
                payload["message"] = (
                    f"搜索结果共 {total_count} 条，当前第 {page}/{total_pages} 页，可使用 page={page + 1} 获取下一页。"
                )
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            error_message = f"获取搜索结果失败: {str(e)}"
            logger.error(f"获取搜索结果失败: {e}", exc_info=True)
            return error_message
