import asyncio
import json
from datetime import datetime
from unittest.mock import patch

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.query_doctor_report import QueryDoctorReportTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.doctor.models import (
    DoctorFinding,
    DoctorFindingStatus,
    DoctorReport,
    DoctorSeverity,
)


def _doctor_report() -> DoctorReport:
    """构造一份稳定的 doctor 测试报告。"""
    report = DoctorReport(
        generated_at=datetime(2026, 6, 12, 12, 0, 0),
        version="v2.test",
        environment={
            "runtime": "Docker",
            "config_path": "/config",
            "is_docker": True,
        },
    )
    report.add_finding(
        DoctorFinding(
            id="logs.moviepilot.recent_errors",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="最近日志存在错误线索",
            detail="ERROR demo Cookie: <REDACTED>",
            recommendation="结合前后的启动日志定位异常。",
            context={"log_file": "/config/logs/moviepilot.log", "matches": 1},
        )
    )
    return report


def test_factory_registers_doctor_report_tool():
    """工具工厂应注册 doctor 诊断报告工具。"""
    with patch(
        "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
        return_value=[],
    ):
        tools = MoviePilotToolFactory.create_tools(
            session_id="doctor-session",
            user_id="10001",
        )

    tool_names = {tool.name for tool in tools}
    assert "query_doctor_report" in tool_names


def test_query_doctor_report_returns_readonly_report():
    """doctor 工具应以只读方式返回结构化诊断报告。"""
    tool = QueryDoctorReportTool(session_id="doctor-session", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_doctor_report.run_doctor",
        return_value=_doctor_report(),
    ) as run_doctor:
        result = asyncio.run(tool.run(deep=True))

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["deep"] is True
    assert payload["include_details"] is True
    assert payload["report"]["status"] == "degraded"
    assert payload["report"]["environment"]["runtime"] == "Docker"
    assert payload["report"]["findings"][0]["detail"] == "ERROR demo Cookie: <REDACTED>"
    assert payload["report"]["findings"][0]["affects_report_status"] is True
    run_doctor.assert_called_once_with(deep=True)


def test_query_doctor_report_compact_mode_omits_details():
    """紧凑模式应保留诊断项概要并省略 detail 和 context。"""
    tool = QueryDoctorReportTool(session_id="doctor-session", user_id="10001")

    with patch(
        "app.agent.tools.impl.query_doctor_report.run_doctor",
        return_value=_doctor_report(),
    ):
        result = asyncio.run(tool.run(include_details=False))

    payload = json.loads(result)
    finding = payload["report"]["findings"][0]
    assert finding["id"] == "logs.moviepilot.recent_errors"
    assert finding["title"] == "最近日志存在错误线索"
    assert finding["affects_report_status"] is True
    assert "detail" not in finding
    assert "context" not in finding


def test_mcp_tool_manager_exposes_doctor_report_tool():
    """MCP 工具管理器应暴露 doctor 诊断报告工具。"""
    tool = QueryDoctorReportTool(session_id="doctor-session", user_id="10001")

    with patch(
        "app.agent.tools.manager.MoviePilotToolFactory.create_tools",
        return_value=[tool],
    ):
        manager = MoviePilotToolsManager(is_admin=True)

    tool_definitions = manager.list_tools()
    assert [item.name for item in tool_definitions] == ["query_doctor_report"]
    schema = tool_definitions[0].input_schema
    assert "deep" in schema["properties"]
    assert "include_details" in schema["properties"]
