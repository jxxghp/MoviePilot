"""查询订阅工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.subscribe import Subscribe as SubscribeSchema
from app.schemas.types import MediaType

PAGE_SIZE = 100

QUERY_SUBSCRIBE_OUTPUT_FIELDS = [
    "id",
    "name",
    "year",
    "type",
    "season",
    "total_episode",
    "start_episode",
    "lack_episode",
    "filter",
    "include",
    "exclude",
    "quality",
    "resolution",
    "effect",
    "state",
    "last_update",
    "sites",
    "downloader",
    "best_version",
    "best_version_full",
    "current_priority",
    "episode_priority",
    "save_path",
    "custom_words",
    "media_category",
    "filter_groups",
    "episode_group",
]


class QuerySubscribesInput(BaseModel):
    """查询订阅工具的输入参数模型"""

    status: Optional[str] = Field(
        "all",
        description="Filter subscriptions by status: 'R' for enabled subscriptions, 'S' for paused ones, 'all' for all subscriptions",
    )
    media_type: Optional[str] = Field(
        "all", description="Allowed values: movie, tv, all"
    )
    tmdb_id: Optional[int] = Field(
        None,
        description="Filter by TMDB ID to check if a specific media is already subscribed",
    )
    douban_id: Optional[str] = Field(
        None,
        description="Filter by Douban ID to check if a specific media is already subscribed",
    )
    bangumi_id: Optional[int] = Field(None, description="Filter by Bangumi ID")
    anilist_id: Optional[int] = Field(None, description="Filter by AniList ID")
    media_source: Optional[str] = Field(None, description="Filter by media source")
    media_id: Optional[str] = Field(None, description="Filter by source-native media ID")
    page: Optional[int] = Field(
        1, description="Page number for pagination (default: 1, 100 items per page)"
    )


class QuerySubscribesTool(MoviePilotTool):
    name: str = "query_subscribes"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Subscription,
    ]
    description: str = "Query subscription status and list user subscriptions. Returns full subscription parameters for each matched subscription. Supports pagination with 100 items per page."
    args_schema: Type[BaseModel] = QuerySubscribesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        status = kwargs.get("status", "all")
        media_type = kwargs.get("media_type", "all")
        page = kwargs.get("page", 1)

        parts = ["查询订阅"]

        # 根据状态过滤条件生成提示
        if status != "all":
            status_map = {"R": "已启用", "S": "已暂停"}
            parts.append(f"状态: {status_map.get(status, status)}")

        # 根据媒体类型过滤条件生成提示
        if media_type != "all":
            parts.append(f"类型: {media_type}")

        parts.append(f"第{page}页")

        return " | ".join(parts)

    async def run(
        self,
        status: Optional[str] = "all",
        media_type: Optional[str] = "all",
        tmdb_id: Optional[int] = None,
        douban_id: Optional[str] = None,
        bangumi_id: Optional[int] = None,
        anilist_id: Optional[int] = None,
        media_source: Optional[str] = None,
        media_id: Optional[str] = None,
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        page = max(1, page or 1)
        logger.info(
            f"执行工具: {self.name}, 参数: status={status}, media_type={media_type}, tmdb_id={tmdb_id}, douban_id={douban_id}, page={page}"
        )
        try:
            if media_type != "all" and not MediaType.from_agent(media_type):
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'all'"

            subscribe_oper = SubscribeOper()
            subscribes = await subscribe_oper.async_list()
            filtered_subscribes = []
            for sub in subscribes:
                if status != "all" and sub.state != status:
                    continue
                if (
                    media_type != "all"
                    and sub.type != MediaType.from_agent(media_type).value
                ):
                    continue
                if tmdb_id is not None and sub.tmdbid != tmdb_id:
                    continue
                if douban_id is not None and sub.doubanid != douban_id:
                    continue
                if bangumi_id is not None and sub.bangumiid != bangumi_id:
                    continue
                if anilist_id is not None and sub.anilistid != anilist_id:
                    continue
                if media_source is not None and sub.media_source != media_source:
                    continue
                if media_id is not None and sub.media_id != media_id:
                    continue
                filtered_subscribes.append(sub)
            if filtered_subscribes:
                total_count = len(filtered_subscribes)
                # 分页
                start = (page - 1) * PAGE_SIZE
                end = start + PAGE_SIZE
                page_subscribes = filtered_subscribes[start:end]

                if not page_subscribes:
                    total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
                    return f"第 {page} 页没有数据，共 {total_count} 条结果，共 {total_pages} 页。"

                full_subscribes = [
                    SubscribeSchema.model_validate(s, from_attributes=True).model_dump(
                        include=set(QUERY_SUBSCRIBE_OUTPUT_FIELDS), exclude_none=True
                    )
                    for s in page_subscribes
                ]
                result_json = json.dumps(full_subscribes, ensure_ascii=False, indent=2)

                total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
                payload_msg = f"第 {page}/{total_pages} 页，当前页 {len(page_subscribes)} 条结果，共 {total_count} 条。"
                if page < total_pages:
                    payload_msg += f" 可使用 page={page + 1} 获取下一页。"

                return f"{payload_msg}\n\n{result_json}"
            return "未找到相关订阅"
        except Exception as e:
            logger.error(f"查询订阅失败: {e}", exc_info=True)
            return f"查询订阅时发生错误: {str(e)}"
