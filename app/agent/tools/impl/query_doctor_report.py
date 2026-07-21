"""查询 MoviePilot Doctor 诊断报告工具。"""

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.doctor import run_doctor
from app.log import logger


class QueryDoctorReportInput(BaseModel):
    """查询 Doctor 诊断报告工具的输入参数模型。"""

    deep: Optional[bool] = Field(
        False,
        description=(
            "Whether to run deeper checks. When true, doctor may perform slower environment probes "
            "such as PostgreSQL TCP connectivity checks."
        ),
    )
    include_details: Optional[bool] = Field(
        True,
        description=(
            "Whether to include full doctor findings with details and context. Set false for a compact "
            "summary when only overall status and finding titles are needed."
        ),
    )


class QueryDoctorReportTool(MoviePilotTool):
    """
    Doctor 离线诊断报告查询工具。
    """

    name: str = "query_doctor_report"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.System,
        ToolTag.Admin,
    ]
    description: str = (
        "Run MoviePilot Doctor in read-only mode and return a structured diagnostic report for troubleshooting. "
        "Use this tool when analyzing startup failures, Docker/runtime issues, port conflicts, dependency problems, "
        "database health, frontend assets, safe mode, or recent log error clues. Plugin-only log findings remain "
        "visible with affects_report_status=false and do not downgrade the overall status. This tool never applies "
        "fixes."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryDoctorReportInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息。"""
        if kwargs.get("deep"):
            return "运行 Doctor 深度诊断"
        return "运行 Doctor 诊断"

    @staticmethod
    def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
        """压缩诊断报告，保留 Agent 判断问题所需的核心字段。"""
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
        """在线程池中运行只读 Doctor 诊断。"""
        return run_doctor(deep=bool(deep)).to_dict()

    async def run(
        self,
        deep: Optional[bool] = False,
        include_details: Optional[bool] = True,
        **kwargs,
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
