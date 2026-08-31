"""本地 CLI 统一 Agent 工具目录测试。"""

from unittest.mock import patch

import pytest
from click import ClickException
from click.testing import CliRunner

from app.cli import _parse_key_value_pairs, cli


def test_tool_arguments_parse_structured_json() -> None:
    """tool run 应支持 API 网关的对象、数组和布尔参数。"""
    assert _parse_key_value_pairs(
        (
            "operation_id=scheduler.run",
            'query={"job_id":"job-1"}',
            "body=[1,2]",
            "enabled=true",
        )
    ) == {
        "operation_id": "scheduler.run",
        "query": {"job_id": "job-1"},
        "body": [1, 2],
        "enabled": True,
    }


def test_tool_arguments_reject_invalid_structured_json() -> None:
    """看似结构化但无效的 JSON 不得静默降级为字符串。"""
    with pytest.raises(ClickException, match="JSON 格式错误"):
        _parse_key_value_pairs(("query={broken}",))


def test_scheduler_cli_uses_final_api_gateway_catalog() -> None:
    """scheduler 子命令不得继续调用已删除的旧工具名。"""
    runner = CliRunner()
    with (
        patch("app.cli._backend_runtime", return_value={}),
        patch(
            "app.cli._call_tool",
            return_value={
                "success": True,
                "message": "",
                "data": [
                    {
                        "id": "job-1",
                        "status": "waiting",
                        "next_run": None,
                        "name": "测试任务",
                    }
                ],
            },
        ) as call_tool,
    ):
        result = runner.invoke(cli, ["scheduler", "list"])

    assert result.exit_code == 0
    assert "job-1" in result.output
    call_tool.assert_called_once_with(
        "moviepilot_api",
        {"operation_id": "scheduler.list"},
        runtime={},
    )


def test_scheduler_run_uses_structured_operation() -> None:
    """scheduler run 应把 job_id 放入固定 operation 的 query。"""
    runner = CliRunner()
    with (
        patch("app.cli._backend_runtime", return_value={}),
        patch(
            "app.cli._call_tool",
            return_value={"success": True, "message": "", "data": True},
        ) as call_tool,
    ):
        result = runner.invoke(cli, ["scheduler", "run", "job-1"])

    assert result.exit_code == 0
    call_tool.assert_called_once_with(
        "moviepilot_api",
        {
            "operation_id": "scheduler.run",
            "query": {"job_id": "job-1"},
        },
        runtime={},
    )
