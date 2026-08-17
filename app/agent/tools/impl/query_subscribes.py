"""查询订阅工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.oper.subscribe import SubscribeOper
from app.runtime.log import logger
from app.schemas.subscribe import Subscribe as SubscribeSchema
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
    media_type_to_agent,
)
from app.domain.media import normalize_music_type

PAGE_SIZE = 100

QUERY_SUBSCRIBE_OUTPUT_FIELDS = [
    "id",
    "name",
    "year",
    "type",
    "media_source",
    "media_id",
    "music_type",
    "total_tracks",
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
    "audio_quality",
    "audio_format",
    "min_bitrate",
    "min_bit_depth",
    "min_sample_rate",
    "state",
    "last_update",
    "sites",
    "downloader",
    "best_version",
    "best_version_full",
    "current_priority",
    "current_audio_format",
    "current_bitrate",
    "current_bit_depth",
    "current_sample_rate",
    "episode_priority",
    "save_path",
    "custom_words",
    "media_category",
    "filter_groups",
    "episode_group",
    "poster",
    "backdrop",
    "description",
    "username",
]


class QuerySubscribesInput(BaseModel):
    """查询订阅工具的输入参数模型"""

    status: Optional[str] = Field(
        "all",
        description="Filter subscriptions by status: 'R' for enabled subscriptions, 'S' for paused ones, 'all' for all subscriptions",
    )
    media_type: Optional[str] = Field(
        "all", description="Allowed values: movie, tv, music, all"
    )
    music_type: Optional[str] = Field(
        None,
        description="Optional music subscription filter: recording or album",
    )
    media_source: Optional[MediaSource] = Field(
        None, description="Filter by media source"
    )
    media_id: Optional[str] = Field(None, description="Filter by source-native media ID")
    page: Optional[int] = Field(
        1, description="Page number for pagination (default: 1, 100 items per page)"
    )


class QuerySubscribesTool(MoviePilotTool):
    """查询电影、电视剧、单曲与专辑订阅。"""

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
        music_type: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        """按状态、媒体身份及音乐实体类型筛选订阅。"""
        page = max(1, page or 1)
        logger.info(
            f"执行工具: {self.name}, 参数: status={status}, "
            f"media_type={media_type}, media_source={media_source}, "
            f"media_id={media_id}, page={page}"
        )
        try:
            if media_type != "all" and not MediaType.from_agent(media_type):
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'music', 'all'"
            normalized_music_type = None
            if music_type:
                normalized_music_type = normalize_music_type(
                    music_type,
                    allow_artist=False,
                )
                if not normalized_music_type:
                    return (
                        f"错误：无效的音乐实体类型 '{music_type}'，"
                        "支持的类型：'recording', 'album'"
                    )
                if media_type not in ("all", "music"):
                    return "错误：music_type 仅能与 media_type='music' 或 'all' 一起使用"

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
                if media_source is not None and sub.media_source != str(media_source):
                    continue
                if media_id is not None and sub.media_id != media_id:
                    continue
                if normalized_music_type:
                    sub_music_type = sub.music_type or MUSIC_ENTITY_RECORDING
                    if sub_music_type != normalized_music_type:
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

                full_subscribes = []
                for subscribe in page_subscribes:
                    payload = SubscribeSchema.model_validate(
                        subscribe,
                        from_attributes=True,
                    ).model_dump(
                        include=set(QUERY_SUBSCRIBE_OUTPUT_FIELDS), exclude_none=True
                    )
                    # 手动总集数是运行锁状态，不属于公共订阅写入 Schema，查询时直接从实体读取。
                    payload["manual_total_episode"] = subscribe.manual_total_episode or 0
                    payload["type"] = media_type_to_agent(payload.get("type"))
                    if payload["type"] == "music" and not payload.get("music_type"):
                        payload["music_type"] = MUSIC_ENTITY_RECORDING
                    full_subscribes.append(payload)
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
