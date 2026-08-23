"""查询整理历史记录工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.agentdata import get_agent_transfer_history_port
from app.runtime.log import logger
from app.schemas.types import media_type_to_agent
from app.foundation.text import cut as jieba_cut


class QueryTransferHistoryInput(BaseModel):
    """查询整理历史记录工具的输入参数模型"""
    title: Optional[str] = Field(None, description="Search by title (optional, supports partial match)")
    status: Optional[str] = Field("all",
                                  description="Filter by status: 'success' for successful transfers, 'failed' for failed transfers, 'all' for all records (default: 'all')")
    page: Optional[int] = Field(1, description="Page number for pagination (default: 1, each page contains 30 records)")


class QueryTransferHistoryTool(MoviePilotTool):
    """查询影视与音乐整理历史及可复用的媒体身份。"""

    name: str = "query_transfer_history"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Transfer,
    ]
    description: str = (
        "Query movie, TV, and music transfer history. Returns source/destination paths, status, errors, and "
        "stable media_source/media_id values that can be reused for retries. Music album retries should group "
        "tracks by the shared album directory/identity instead of treating every track as unrelated media."
    )
    args_schema: Type[BaseModel] = QueryTransferHistoryInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        title = kwargs.get("title")
        status = kwargs.get("status", "all")
        page = kwargs.get("page", 1)

        parts = ["查询整理历史"]

        if title:
            parts.append(f"标题: {title}")
        if status != "all":
            status_map = {"success": "成功", "failed": "失败"}
            parts.append(f"状态: {status_map.get(status, status)}")
        if page > 1:
            parts.append(f"第{page}页")

        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, title: Optional[str] = None,
                  status: Optional[str] = "all",
                  page: Optional[int] = 1, **kwargs) -> str:
        """执行整理历史筛选并返回有界分页结果。"""
        logger.info(f"执行工具: {self.name}, 参数: title={title}, status={status}, page={page}")

        try:
            # 处理状态参数
            status_bool = None
            if status == "success":
                status_bool = True
            elif status == "failed":
                status_bool = False

            # 处理页码参数
            if page is None or page < 1:
                page = 1

            # 每页固定 30 条，与工具说明保持一致，避免整理路径等字段撑大上下文。
            count = 30

            transferhis = get_agent_transfer_history_port()
            # 处理标题搜索
            if title:
                # 使用统一分词封装处理标题，便于替换底层实现。
                words = jieba_cut(title, HMM=False)
                title_search = "%".join(words)
                # 查询记录
                result = await transferhis.async_list_by_title(
                    title=title_search, page=page, count=count, status=status_bool
                )
                total = await transferhis.async_count_by_title(
                    title=title_search, status=status_bool
                )
            else:
                # 查询所有记录
                result = await transferhis.async_list_by_page(
                    page=page, count=count, status=status_bool
                )
                total = await transferhis.async_count(status=status_bool)

            if not result:
                return "未找到相关整理历史记录"

            # 转换为字典格式，只保留关键信息
            simplified_records = []
            for record in result:
                simplified = {
                    "id": record.id,
                    "title": record.title,
                    "year": record.year,
                    "type": media_type_to_agent(record.type),
                    "category": record.category,
                    "seasons": record.seasons,
                    "episodes": record.episodes,
                    "src": record.src,
                    "dest": record.dest,
                    "mode": record.mode,
                    "status": "成功" if record.status else "失败",
                    "date": record.date,
                    "downloader": record.downloader,
                    "download_hash": record.download_hash
                }
                # 如果失败，添加错误信息
                if not record.status and record.errmsg:
                    simplified["errmsg"] = record.errmsg
                if record.media_source:
                    simplified["media_source"] = record.media_source
                if record.media_id:
                    simplified["media_id"] = record.media_id
                if getattr(record, "music_type", None):
                    simplified["music_type"] = record.music_type
                if getattr(record, "total_tracks", None):
                    simplified["total_tracks"] = record.total_tracks
                simplified_records.append(simplified)

            result_json = json.dumps(simplified_records, ensure_ascii=False, indent=2)

            # 计算总页数
            total_pages = (total + count - 1) // count if total > 0 else 1

            # 构建分页信息
            pagination_info = f"第 {page}/{total_pages} 页，共 {total} 条记录（每页 {count} 条）"

            return f"{pagination_info}\n\n{result_json}"
        except Exception as e:
            logger.error(f"查询整理历史记录失败: {e}", exc_info=True)
            return f"查询整理历史记录时发生错误: {str(e)}"
