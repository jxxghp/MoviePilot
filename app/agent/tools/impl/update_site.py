"""更新站点工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.events import eventmanager
from app.application.agentdata import get_agent_site_port
from app.runtime.log import logger
from app.schemas.types import EventType
from app.foundation import url as url_tools


class UpdateSiteInput(BaseModel):
    """更新站点工具的输入参数模型"""

    site_id: int = Field(
        ...,
        description="The ID of the site to update (can be obtained from query_sites tool)",
    )
    name: Optional[str] = Field(None, description="Site name (optional)")
    url: Optional[str] = Field(
        None, description="Site URL (optional, will be automatically formatted)"
    )
    pri: Optional[int] = Field(
        None,
        description="Site priority (optional, smaller value = higher priority, e.g., pri=1 has higher priority than pri=10)",
    )
    rss: Optional[str] = Field(None, description="RSS feed URL (optional)")
    cookie: Optional[str] = Field(None, description="Site cookie (optional)")
    ua: Optional[str] = Field(None, description="User-Agent string (optional)")
    apikey: Optional[str] = Field(None, description="API key (optional)")
    token: Optional[str] = Field(None, description="API token (optional)")
    proxy: Optional[int] = Field(
        None, description="Whether to use proxy: 0 for no, 1 for yes (optional)"
    )
    filter: Optional[str] = Field(
        None, description="Filter rule as regular expression (optional)"
    )
    note: Optional[str] = Field(None, description="Site notes/remarks (optional)")
    timeout: Optional[int] = Field(
        None, description="Request timeout in seconds (optional, default: 15)"
    )
    limit_interval: Optional[int] = Field(
        None, description="Rate limit interval in seconds (optional)"
    )
    limit_count: Optional[int] = Field(
        None, description="Rate limit count per interval (optional)"
    )
    limit_seconds: Optional[int] = Field(
        None, description="Rate limit seconds between requests (optional)"
    )
    is_active: Optional[bool] = Field(
        None,
        description="Whether site is active: True for enabled, False for disabled (optional)",
    )
    downloader: Optional[str] = Field(
        None, description="Downloader name for this site (optional)"
    )


class UpdateSiteTool(MoviePilotTool):
    name: str = "update_site"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Site,
        ToolTag.Admin,
    ]
    description: str = "Update site configuration including URL, priority, authentication credentials (cookie, UA, API key), proxy settings, rate limits, and other site properties. Supports updating multiple site attributes at once. Site priority (pri): smaller values have higher priority (e.g., pri=1 has higher priority than pri=10)."
    args_schema: Type[BaseModel] = UpdateSiteInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据更新参数生成友好的提示消息"""
        site_id = kwargs.get("site_id")
        fields_updated = []

        if kwargs.get("name"):
            fields_updated.append("名称")
        if kwargs.get("url"):
            fields_updated.append("URL")
        if kwargs.get("pri") is not None:
            fields_updated.append("优先级")
        if kwargs.get("cookie"):
            fields_updated.append("Cookie")
        if kwargs.get("ua"):
            fields_updated.append("User-Agent")
        if kwargs.get("proxy") is not None:
            fields_updated.append("代理设置")
        if kwargs.get("is_active") is not None:
            fields_updated.append("启用状态")
        if kwargs.get("downloader"):
            fields_updated.append("下载器")

        if fields_updated:
            return f"更新站点 #{site_id}: {', '.join(fields_updated)}"
        return f"更新站点 #{site_id}"

    async def run(
        self,
        site_id: int,
        name: Optional[str] = None,
        url: Optional[str] = None,
        pri: Optional[int] = None,
        rss: Optional[str] = None,
        cookie: Optional[str] = None,
        ua: Optional[str] = None,
        apikey: Optional[str] = None,
        token: Optional[str] = None,
        proxy: Optional[int] = None,
        filter: Optional[str] = None,
        note: Optional[str] = None,
        timeout: Optional[int] = None,
        limit_interval: Optional[int] = None,
        limit_count: Optional[int] = None,
        limit_seconds: Optional[int] = None,
        is_active: Optional[bool] = None,
        downloader: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, 参数: site_id={site_id}")

        try:
            site_oper = get_agent_site_port()
            site = await site_oper.async_get(site_id)
            if not site:
                return json.dumps(
                    {"success": False, "message": f"站点不存在: {site_id}"},
                    ensure_ascii=False,
                )

            # 构建更新字典
            site_dict = {}

            # 基本信息
            if name is not None:
                site_dict["name"] = name

            # URL处理（需要校正格式）
            if url is not None:
                _scheme, _netloc = url_tools.split_netloc(url)
                site_dict["url"] = f"{_scheme}://{_netloc}/"

            if pri is not None:
                site_dict["pri"] = pri
            if rss is not None:
                site_dict["rss"] = rss

            # 认证信息
            if cookie is not None:
                site_dict["cookie"] = cookie
            if ua is not None:
                site_dict["ua"] = ua
            if apikey is not None:
                site_dict["apikey"] = apikey
            if token is not None:
                site_dict["token"] = token

            # 配置选项
            if proxy is not None:
                site_dict["proxy"] = proxy
            if filter is not None:
                site_dict["filter"] = filter
            if note is not None:
                site_dict["note"] = note
            if timeout is not None:
                site_dict["timeout"] = timeout

            # 流控设置
            if limit_interval is not None:
                site_dict["limit_interval"] = limit_interval
            if limit_count is not None:
                site_dict["limit_count"] = limit_count
            if limit_seconds is not None:
                site_dict["limit_seconds"] = limit_seconds

            # 状态和下载器
            if is_active is not None:
                site_dict["is_active"] = is_active
            if downloader is not None:
                site_dict["downloader"] = downloader

            # 如果没有要更新的字段
            if not site_dict:
                return json.dumps(
                    {"success": False, "message": "没有提供要更新的字段"},
                    ensure_ascii=False,
                )

            # 更新站点
            await site_oper.async_update(site_id, site_dict)

            # 重新获取更新后的站点数据
            updated_site = await site_oper.async_get(site_id)

            # 发送站点更新事件
            await eventmanager.async_send_event(
                EventType.SiteUpdated,
                {"domain": updated_site.domain if updated_site else site.domain},
            )

            # 构建返回结果
            result = {
                "success": True,
                "message": f"站点 #{site_id} 更新成功",
                "site_id": site_id,
                "updated_fields": list(site_dict.keys()),
            }

            if updated_site:
                result["site"] = {
                    "id": updated_site.id,
                    "name": updated_site.name,
                    "domain": updated_site.domain,
                    "url": updated_site.url,
                    "pri": updated_site.pri,
                    "is_active": updated_site.is_active,
                    "downloader": updated_site.downloader,
                    "proxy": updated_site.proxy,
                    "timeout": updated_site.timeout,
                }

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"更新站点失败: {str(e)}"
            logger.error(f"更新站点失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": error_message, "site_id": site_id},
                ensure_ascii=False,
            )
