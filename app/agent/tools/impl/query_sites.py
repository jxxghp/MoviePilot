"""查询站点工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.agentdata import get_agent_site_port
from app.runtime.log import logger


class QuerySitesInput(BaseModel):
    """查询站点工具的输入参数模型"""

    status: Optional[str] = Field(
        "all",
        description="Filter sites by status: 'active' for enabled sites, 'inactive' for disabled sites, 'all' for all sites",
    )
    name: Optional[str] = Field(
        None, description="Filter sites by name (partial match, optional)"
    )


class QuerySitesTool(MoviePilotTool):
    name: str = "query_sites"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Site,
    ]
    description: str = (
        "Query site status and list configured sites. Non-admin users receive a safe view "
        "that omits sensitive fields: cookie, token, API key and RSS URL. "
        "Site priority (pri): smaller values have higher priority (e.g., pri=1 has higher priority than pri=10)."
    )
    args_schema: Type[BaseModel] = QuerySitesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        status = kwargs.get("status", "all")
        name = kwargs.get("name")

        parts = ["查询站点"]

        if status != "all":
            status_map = {"active": "已启用", "inactive": "已禁用"}
            parts.append(f"状态: {status_map.get(status, status)}")

        if name:
            parts.append(f"名称: {name}")

        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(
        self, status: Optional[str] = "all", name: Optional[str] = None, **kwargs
    ) -> str:
        logger.info(f"执行工具: {self.name}, 参数: status={status}, name={name}")
        try:
            is_admin = await self.is_admin_user()
            site_oper = get_agent_site_port()
            # 获取所有站点（按优先级排序）
            sites = await site_oper.async_list()
            filtered_sites = []
            for site in sites:
                # 按状态过滤
                if status == "active" and not site.is_active:
                    continue
                if status == "inactive" and site.is_active:
                    continue
                # 按名称过滤（部分匹配）
                if name and name.lower() not in (site.name or "").lower():
                    continue
                filtered_sites.append(site)
            if filtered_sites:
                # 精简字段，只保留关键信息
                simplified_sites = []
                for s in filtered_sites:
                    simplified = {
                        "id": s.id,
                        "name": s.name,
                        "domain": s.domain,
                        "url": s.url,
                        "pri": s.pri,
                        "is_active": s.is_active,
                        "downloader": s.downloader,
                        "ua": s.ua,
                        "proxy": s.proxy,
                        "filter": s.filter,
                        "render": s.render,
                        "public": s.public,
                        "note": s.note,
                        "limit_interval": s.limit_interval,
                        "limit_count": s.limit_count,
                        "limit_seconds": s.limit_seconds,
                        "timeout": s.timeout,
                    }
                    if is_admin:
                        simplified.update({
                            "rss": s.rss,
                            "cookie": s.cookie,
                            "apikey": s.apikey,
                            "token": s.token,
                        })
                    simplified_sites.append(simplified)
                result_json = json.dumps(simplified_sites, ensure_ascii=False, indent=2)
                return result_json
            return "未找到相关站点"
        except Exception as e:
            logger.error(f"查询站点失败: {e}", exc_info=True)
            return f"查询站点时发生错误: {str(e)}"
