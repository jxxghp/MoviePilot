"""查询订阅历史工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.subscribehistory_oper import SubscribeHistoryOper
from app.log import logger
from app.schemas.types import media_type_to_agent

PAGE_SIZE = 20


class QuerySubscribeHistoryInput(BaseModel):
    """查询订阅历史工具的输入参数模型"""

    media_type: Optional[str] = Field(
        "all", description="Allowed values: movie, tv, all"
    )
    name: Optional[str] = Field(
        None, description="Filter by media name (partial match, optional)"
    )
    page: Optional[int] = Field(
        1,
        description="Page number for pagination (default: 1, 20 items per page). Ignored when name filter is provided.",
    )


class QuerySubscribeHistoryTool(MoviePilotTool):
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
        name: Optional[str] = None,
        page: Optional[int] = 1,
        **kwargs,
    ) -> str:
        page = max(1, page or 1)
        logger.info(
            f"执行工具: {self.name}, 参数: media_type={media_type}, name={name}, page={page}"
        )

        try:
            if media_type not in ["all", "movie", "tv"]:
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'all'"

            subscribe_history_oper = SubscribeHistoryOper()
            if name:
                # 有名称过滤时，获取足够多的记录在内存中过滤，不分页
                fetch_count = 500
                if media_type == "all":
                    movie_history = await subscribe_history_oper.async_list_by_type(
                        mtype="movie", page=1, count=fetch_count
                    )
                    tv_history = await subscribe_history_oper.async_list_by_type(
                        mtype="tv", page=1, count=fetch_count
                    )
                    all_history = list(movie_history) + list(tv_history)
                    all_history.sort(key=lambda x: x.date or "", reverse=True)
                else:
                    all_history = list(
                        await subscribe_history_oper.async_list_by_type(
                            mtype=media_type, page=1, count=fetch_count
                        )
                    )

                # 按名称过滤
                name_lower = name.lower()
                filtered_history = [
                    record
                    for record in all_history
                    if record.name and name_lower in record.name.lower()
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
                if media_type == "all":
                    movie_history = await subscribe_history_oper.async_list_by_type(
                        mtype="movie", page=1, count=page * PAGE_SIZE
                    )
                    tv_history = await subscribe_history_oper.async_list_by_type(
                        mtype="tv", page=1, count=page * PAGE_SIZE
                    )
                    all_history = list(movie_history) + list(tv_history)
                    all_history.sort(key=lambda x: x.date or "", reverse=True)
                    filtered_history = all_history
                else:
                    filtered_history = list(
                        await subscribe_history_oper.async_list_by_type(
                            mtype=media_type, page=1, count=page * PAGE_SIZE
                        )
                    )

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
                "tmdbid": record.tmdbid,
                "doubanid": record.doubanid,
                "bangumiid": record.bangumiid,
                "anilistid": record.anilistid,
                "media_source": record.media_source,
                "media_id": record.media_id,
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
