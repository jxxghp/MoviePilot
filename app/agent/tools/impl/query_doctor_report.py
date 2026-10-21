"""
查询 MoviePilot Doctor 诊断报告工具。
"""

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.doctor.runner import run_doctor
from app.runtime.log import logger


class QueryDoctorReportInput(BaseModel):
    """
    查询 Doctor 诊断报告工具的输入参数模型。
    """

    deep: Optional[bool] = Field(
        False,
        description="是否执行较深检查，启用时可能进行 PostgreSQL TCP 连通性等较慢的环境探测",
    )
    include_details: Optional[bool] = Field(
        True,
        description="是否包含完整诊断详情和上下文；只需总体状态和发现标题时可设为 False",
    )


class QueryDoctorReportTool(MoviePilotTool):
    """
    Doctor 离线诊断报告查询工具。
    """

    name: str = Field(
        default="query_doctor_report",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Read, ToolTag.System, ToolTag.Admin],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "以只读模式运行 MoviePilot Doctor，返回用于排障的结构化报告。"
            "适用于启动失败、Docker 或运行环境、端口冲突、依赖、数据库健康、前端资源、安全模式及近期日志错误线索。"
            "仅涉及插件的日志发现仍会展示，并标记 affects_report_status=false，不降低总体状态。"
            "本工具不执行修复。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )
    args_schema: Type[BaseModel] = Field(
        default=QueryDoctorReportInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """
        根据查询参数生成友好的提示消息。
        """
        if kwargs.get("deep"):
            return "运行 Doctor 深度诊断"
        return "运行 Doctor 诊断"

    @staticmethod
    def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
        """
        压缩诊断报告，保留 Agent 判断问题所需的核心字段。
        """
        return {
            "schema_version": report.get("schema_version"),
            "status": report.get("status"),
            "generated_at": report.get("generated_at"),
            "version": report.get("version"),
            "environment": report.get("environment"),
            "summary": report.get("summary"),
            "findings": [
                {
                    "id": item.get("id"),
                    "severity": item.get("severity"),
                    "status": item.get("status"),
                    "title": item.get("title"),
                    "fixable": item.get("fixable"),
                    "fixed": item.get("fixed"),
                    "affects_report_status": item.get("affects_report_status", True),
                }
                for item in report.get("findings") or []
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _run_doctor_report(deep: bool = False) -> dict[str, Any]:
        """
        在线程池中运行只读 Doctor 诊断。
        """
        return run_doctor(deep=bool(deep)).to_dict()

    async def run(  # type: ignore[override]
        self,
        deep: Optional[bool] = False,
        include_details: Optional[bool] = True,
        **kwargs: object,
    ) -> str:
        """
        运行只读 Doctor 诊断并返回 JSON 字符串。
        """
        logger.info(
            f"执行工具: {self.name}, deep={bool(deep)}, include_details={bool(include_details)}"
        )
        try:
            report = await self.run_blocking("default", self._run_doctor_report, bool(deep))
            if not include_details:
                report = self._compact_report(report)
            return json.dumps(
                {
                    "success": True,
                    "deep": bool(deep),
                    "include_details": bool(include_details),
                    "report": report,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception as err:
            logger.error(f"查询 Doctor 诊断报告失败: {err}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"查询 Doctor 诊断报告时发生错误: {str(err)}",
                },
                ensure_ascii=False,
            )
