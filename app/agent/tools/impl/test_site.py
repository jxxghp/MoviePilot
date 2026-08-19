"""测试站点连通性工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.orchestration.site import SiteChain
from app.application.agentdata import SitePort as SiteOper
from app.runtime.log import logger


class TestSiteInput(BaseModel):
    """测试站点连通性工具的输入参数模型"""
    site_identifier: int = Field(..., description="Site ID to test (can be obtained from query_sites tool)")


class TestSiteTool(MoviePilotTool):
    name: str = "test_site"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Site,
    ]
    description: str = "Test site connectivity and availability. This will check if a site is accessible and can be logged in. Accepts site ID only."
    args_schema: Type[BaseModel] = TestSiteInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据测试参数生成友好的提示消息"""
        site_identifier = kwargs.get("site_identifier")
        return f"测试站点连通性: {site_identifier}"

    @staticmethod
    def _test_site_sync(site_identifier: int) -> tuple[Optional[str], Optional[str], bool, str]:
        """在同步线程里执行站点联通测试，避免网络请求卡住事件循环。"""
        site = SiteOper().get(site_identifier)
        if not site:
            return None, None, False, f"未找到站点：{site_identifier}，请使用 query_sites 工具查询可用的站点"

        status, message = SiteChain().test(site.domain)
        return site.name, site.domain, status, message

    async def run(self, site_identifier: int, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: site_identifier={site_identifier}")

        try:
            site_name, site_domain, status, message = await self.run_blocking(
                "site", self._test_site_sync, site_identifier
            )
            if not site_name:
                return message
            if status:
                return f"站点连通性测试成功：{site_name} ({site_domain})\n{message}"
            else:
                return f"站点连通性测试失败：{site_name} ({site_domain})\n{message}"
        except Exception as e:
            logger.error(f"测试站点连通性失败: {e}", exc_info=True)
            return f"测试站点连通性时发生错误: {str(e)}"
