"""查询订阅历史工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaType, media_type_to_agent
from app.domain.media import normalize_music_type

PAGE_SIZE = 20


class QuerySubscribeHistoryInput(BaseModel):
    """查询订阅历史工具的输入参数模型"""

    media_type: Optional[str] = Field(
        "all", description="Allowed values: movie, tv, music, all"
    )
    music_type: Optional[str] = Field(
        None,
        description="Optional music history filter: recording or album",
    )
    name: Optional[str] = Field(
        None, description="Filter by media name (partial match, optional)"
    )
    page: Optional[int] = Field(
        1,
        description="Page number for pagination (default: 1, 20 items per page). Ignored when name filter is provided.",
    )


class QuerySubscribeHistoryTool(MoviePilotTool):
    """查询已完成的影视、单曲与整张专辑订阅历史。"""

    name: str = "query_subscribe_history"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Subscription,
    ]
    description: str = "Query subscription history records. Shows completed subscriptions with their details including name, type, rating, completion date, and other subscription information. Supports filtering by media type and name. Supports pagination with 20 records per page."
    args_schema: Type[BaseModel] = QuerySubscribeHistoryInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        media_type = kwargs.get("media_type", "all")
        name = kwargs.get("name")
        page = kwargs.get("page", 1)

        parts = ["查询订阅历史"]

        if media_type != "all":
            parts.append(f"类型: {media_type}")
        if name:
            parts.append(f"名称: {name}")
        else:
            parts.append(f"第{page}页")

        return " | ".join(parts)

    async def run(
        self,
        media_type: Optional[str] = "all",
        music_type: Optional[str] = None,
        name: Optional[str] = None,
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        """按规范化数据库类型查询并合并订阅历史。"""
        page = max(1, page or 1)
        logger.info(
            f"执行工具: {self.name}, 参数: media_type={media_type}, name={name}, page={page}"
        )

        try:
            if media_type == "all":
                requested_types = [
                    MediaType.MOVIE.value,
                    MediaType.TV.value,
                    MediaType.MUSIC.value,
                ]
            else:
                media_type_enum = MediaType.from_agent(media_type)
                if not media_type_enum:
                    return (
                        f"错误：无效的媒体类型 '{media_type}'，"
                        "支持的类型：'movie', 'tv', 'music', 'all'"
                    )
                requested_types = [media_type_enum.value]

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
                if MediaType.MUSIC.value not in requested_types:
                    return "错误：music_type 仅能与 media_type='music' 或 'all' 一起使用"

            subscribe_history_oper = SubscribeHistoryOper()
            if name:
                # 有名称过滤时，获取足够多的记录在内存中过滤，不分页
                fetch_count = 500
                history_groups = [
                    await subscribe_history_oper.async_list_by_type(
                        mtype=requested_type,
                        page=1,
                        count=fetch_count,
                    )
                    for requested_type in requested_types
                ]
                all_history = [
                    record for group in history_groups for record in group
                ]
                all_history.sort(key=lambda x: x.date or "", reverse=True)

                # 按名称过滤
                name_lower = name.lower()
                filtered_history = [
                    record
                    for record in all_history
                    if record.name and name_lower in record.name.lower()
                    and self._matches_music_type(record, normalized_music_type)
                ]

                if not filtered_history:
                    return "未找到相关订阅历史记录"

                # 名称过滤时直接返回所有匹配结果，不分页
                simplified_records = self._simplify_records(filtered_history)
                result_json = json.dumps(
                    simplified_records, ensure_ascii=False, indent=2
                )
                return result_json
            else:
                # 无名称过滤时，直接利用数据库分页
                history_groups = [
                    await subscribe_history_oper.async_list_by_type(
                        mtype=requested_type,
                        page=1,
                        count=page * PAGE_SIZE,
                    )
                    for requested_type in requested_types
                ]
                all_history = [
                    record for group in history_groups for record in group
                ]
                all_history.sort(key=lambda x: x.date or "", reverse=True)
                filtered_history = [
                    record
                    for record in all_history
                    if self._matches_music_type(record, normalized_music_type)
                ]

            if not filtered_history:
                return "未找到相关订阅历史记录"

            # 分页切片
            total_count = len(filtered_history)
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_records = filtered_history[start:end]

            if not page_records:
                return f"第 {page} 页没有数据。"

            simplified_records = self._simplify_records(page_records)
            result_json = json.dumps(
                simplified_records, ensure_ascii=False, indent=2
            )

            has_more = total_count > end
            payload_msg = f"第 {page} 页，当前页 {len(simplified_records)} 条结果。"
            if has_more:
                payload_msg += (
                    f" 可能有更多数据，可使用 page={page + 1} 获取下一页。"
                )

            return f"{payload_msg}\n\n{result_json}"
        except Exception as e:
            logger.error(f"查询订阅历史失败: {e}", exc_info=True)
            return f"查询订阅历史时发生错误: {str(e)}"

    @staticmethod
    def _matches_music_type(record, music_type: Optional[str]) -> bool:
        """匹配音乐实体类型，旧空值历史按单曲兼容。"""
        if not music_type:
            return True
        if media_type_to_agent(getattr(record, "type", None)) != "music":
            return False
        return (getattr(record, "music_type", None) or MUSIC_ENTITY_RECORDING) == music_type

    @staticmethod
    def _simplify_records(records) -> list:
        """转换为字典格式，只保留关键信息"""
        simplified_records = []
        for record in records:
            simplified = {
                "id": record.id,
                "name": record.name,
                "year": record.year,
                "type": media_type_to_agent(record.type),
                "season": record.season,
                "media_source": record.media_source,
                "media_id": record.media_id,
                "music_type": getattr(record, "music_type", None) or (
                    MUSIC_ENTITY_RECORDING if media_type_to_agent(record.type) == "music" else None
                ),
                "total_tracks": getattr(record, "total_tracks", None),
                "poster": record.poster,
                "vote": record.vote,
                "total_episode": record.total_episode,
                "date": record.date,
                "username": record.username,
            }
            if record.filter:
                simplified["filter"] = record.filter
            if record.quality:
                simplified["quality"] = record.quality
            if record.resolution:
                simplified["resolution"] = record.resolution
            simplified_records.append(simplified)
        return simplified_records
