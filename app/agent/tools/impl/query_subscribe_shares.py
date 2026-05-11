"""查询订阅分享工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.helper.subscribe import SubscribeHelper
from app.log import logger

MAX_PAGE_SIZE = 50


class QuerySubscribeSharesInput(BaseModel):
    """查询订阅分享工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    name: Optional[str] = Field(None, description="Filter shares by media name (partial match, optional)")
    page: Optional[int] = Field(1, description="Page number for pagination (default: 1)")
    count: Optional[int] = Field(30, description="Number of items per page (default: 30, max: 50)")
    genre_id: Optional[int] = Field(None, description="Filter by genre ID (optional)")
    min_rating: Optional[float] = Field(None, description="Minimum rating filter (optional, e.g., 7.5)")
    max_rating: Optional[float] = Field(None, description="Maximum rating filter (optional, e.g., 10.0)")
    sort_type: Optional[str] = Field(None, description="Sort type (optional, e.g., 'count', 'rating')")


class QuerySubscribeSharesTool(MoviePilotTool):
    name: str = "query_subscribe_shares"
    description: str = "Query shared subscriptions from other users. Shows popular subscriptions shared by the community with filtering and pagination support."
    args_schema: Type[BaseModel] = QuerySubscribeSharesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        name = kwargs.get("name")
        page = kwargs.get("page", 1)
        min_rating = kwargs.get("min_rating")
        max_rating = kwargs.get("max_rating")
        
        parts = ["查询订阅分享"]
        
        if name:
            parts.append(f"名称: {name}")
        if min_rating:
            parts.append(f"最低评分: {min_rating}")
        if max_rating:
            parts.append(f"最高评分: {max_rating}")
        if page > 1:
            parts.append(f"第{page}页")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, name: Optional[str] = None,
                  page: Optional[int] = 1,
                  count: Optional[int] = 30,
                  genre_id: Optional[int] = None,
                  min_rating: Optional[float] = None,
                  max_rating: Optional[float] = None,
                  sort_type: Optional[str] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: name={name}, page={page}, count={count}, genre_id={genre_id}, "
            f"min_rating={min_rating}, max_rating={max_rating}, sort_type={sort_type}")

        try:
            if page is None or page < 1:
                page = 1
            if count is None or count < 1:
                count = 30
            # 订阅分享是外部列表型结果，限制单页大小能降低工具上下文占用。
            count = min(count, MAX_PAGE_SIZE)

            subscribe_helper = SubscribeHelper()
            shares = await subscribe_helper.async_get_shares(
                name=name,
                page=page,
                count=count,
                genre_id=genre_id,
                min_rating=min_rating,
                max_rating=max_rating,
                sort_type=sort_type
            )

            if not shares:
                return "未找到订阅分享数据（可能订阅分享功能未启用）"

            # 简化字段，只保留关键信息
            simplified_shares = []
            for share in shares:
                simplified = {
                    "id": share.get("id"),
                    "name": share.get("name"),
                    "year": share.get("year"),
                    "type": share.get("type"),
                    "season": share.get("season"),
                    "tmdbid": share.get("tmdbid"),
                    "doubanid": share.get("doubanid"),
                    "bangumiid": share.get("bangumiid"),
                    "poster": share.get("poster"),
                    "vote": share.get("vote"),
                    "share_title": share.get("share_title"),
                    "share_comment": share.get("share_comment"),
                    "share_user": share.get("share_user"),
                    "fork_count": share.get("fork_count", 0)
                }
                # 截断过长的描述
                if simplified.get("description") and len(simplified["description"]) > 200:
                    simplified["description"] = simplified["description"][:200] + "..."
                simplified_shares.append(simplified)

            result_json = json.dumps(simplified_shares, ensure_ascii=False, indent=2)

            pagination_info = f"第 {page} 页，每页 {count} 条，共 {len(simplified_shares)} 条结果"

            return f"{pagination_info}\n\n{result_json}"
        except Exception as e:
            logger.error(f"查询订阅分享失败: {e}", exc_info=True)
            return f"查询订阅分享时发生错误: {str(e)}"
