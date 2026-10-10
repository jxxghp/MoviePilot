"""本地 CLI 统一 Agent 工具目录测试。"""

import json
from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest
from click import ClickException
from click.testing import CliRunner

from app.cli import _parse_key_value_pairs, cli


@pytest.fixture
def http_request() -> Iterator[Mock]:
    """只替换 HTTP 边界，保留工具信封解析与命令渲染。"""
    with (
        patch("app.cli._backend_runtime", return_value={}),
        patch("app.cli._runtime_api_token", return_value="cli-test-token"),
        patch("app.cli._http_request") as request,
    ):
        yield request


def _http_response(data: object, status: int = 200) -> dict:
    """构造与 MCP REST 端点一致的统一成功信封。"""
    payload = {"success": True, "message": "", "data": data}
    return {"status": status, "json": payload, "text": json.dumps(payload)}


def test_tool_list_unwraps_rest_envelope(http_request: Mock) -> None:
    """工具列表应解包后按名称排序，不能把整个成功信封当错误。"""
    http_request.return_value = _http_response([{"name": "read_skill"}, {"name": "moviepilot_api"}])

    result = CliRunner().invoke(cli, ["tool", "list"])

    assert result.exit_code == 0
    assert result.output == "moviepilot_api\nread_skill\n"


def test_tool_show_unwraps_rest_envelope(http_request: Mock) -> None:
    """工具详情必须展示 data 内的名称、描述及参数。"""
    http_request.return_value = _http_response({
        "name": "moviepilot_api",
        "description": "统一 API 网关",
        "inputSchema": {
            "properties": {"operation_id": {"type": "string", "description": "操作 ID"}},
            "required": ["operation_id"],
        },
    })

    result = CliRunner().invoke(cli, ["tool", "show", "moviepilot_api"])

    assert result.exit_code == 0
    assert "Command: moviepilot_api" in result.output
    assert "Description: 统一 API 网关" in result.output
    assert "operation_id*" in result.output
    assert "string" in result.output
    assert "操作 ID" in result.output


@pytest.mark.parametrize("tool_result", ['{"version":"v3.1.3"}', '[1,2]', "执行完成", "false", "null", ""])
def test_tool_run_unwraps_and_prints_result(http_request: Mock, tool_result: str) -> None:
    """执行结果保留 JSON 或文本语义，空结果也必须有明确回显。"""
    http_request.return_value = _http_response({"result": tool_result}, status=201)

    result = CliRunner().invoke(cli, ["tool", "run", "moviepilot_api", "operation_id=system.versions"])

    assert result.exit_code == 0
    assert result.output.strip()
    if tool_result.startswith(("{", "[")):
        assert json.loads(result.output) == json.loads(tool_result)
    elif tool_result == "false":
        assert result.output == "False\n"
    elif tool_result in {"null", ""}:
        assert result.output == "工具执行成功（无返回内容）\n"
    else:
        assert result.output == f"{tool_result}\n"


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


def test_scheduler_cli_uses_final_api_gateway_catalog(http_request: Mock) -> None:
    """scheduler 子命令不得继续调用已删除的旧工具名。"""
    runner = CliRunner()
    http_request.return_value = _http_response({"result": json.dumps({
        "success": True,
        "message": "",
        "data": [{"id": "job-1", "status": "waiting", "next_run": None, "name": "测试任务"}],
    })})
    result = runner.invoke(cli, ["scheduler", "list"])

    assert result.exit_code == 0
    assert result.output == "job-1\twaiting\tNone\t测试任务\n"
    http_request.assert_called_once_with(
        "POST",
        "/api/v1/mcp/tools/call",
        json_body={"tool_name": "moviepilot_api", "arguments": {"operation_id": "scheduler.list"}},
        headers={"X-API-KEY": "cli-test-token"},
        timeout=30.0,
        runtime={},
    )


def test_scheduler_run_uses_structured_operation(http_request: Mock) -> None:
    """scheduler run 应把 job_id 放入固定 operation 的 query。"""
    runner = CliRunner()
    http_request.return_value = _http_response({
        "result": json.dumps({"success": True, "message": "", "data": True})
    })
    result = runner.invoke(cli, ["scheduler", "run", "job-1"])

    assert result.exit_code == 0
    assert result.output == "True\n"
    http_request.assert_called_once_with(
        "POST",
        "/api/v1/mcp/tools/call",
        json_body={
            "tool_name": "moviepilot_api",
            "arguments": {"operation_id": "scheduler.run", "query": {"job_id": "job-1"}},
        },
        headers={"X-API-KEY": "cli-test-token"},
        timeout=30.0,
        runtime={},
    )


@pytest.mark.parametrize("command", [
    ["tool", "list"],
    ["tool", "show", "moviepilot_api"],
    ["tool", "run", "moviepilot_api"],
    ["scheduler", "list"],
    ["scheduler", "run", "job-1"],
])
@pytest.mark.parametrize("status, payload, expected", [
    (200, {"success": False, "message": "工具不可用", "data": None}, "工具不可用"),
    (401, {"success": False, "message": "认证失败", "data": None}, "认证失败"),
    (500, {"detail": "服务异常"}, "服务异常"),
    (502, None, "响应格式错误"),
    (200, {"data": ["huge directory"]}, "HTTP 200"),
])
def test_tool_commands_report_http_failures(
    http_request: Mock, command: list[str], status: int, payload: object, expected: str,
) -> None:
    """传输或信封错误必须明确失败，且不得倾倒完整响应正文。"""
    http_request.return_value = {"status": status, "json": payload, "text": "huge response" * 40000}

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 1
    assert expected in result.output
    assert "huge response" not in result.output
    assert len(result.output) < 200
    http_request.assert_called_once()


@pytest.mark.parametrize("command, data, expected", [
    (["tool", "list"], {}, "不是工具列表"),
    (["tool", "show", "moviepilot_api"], [], "不是工具对象"),
    (["tool", "run", "moviepilot_api"], {}, "data.result"),
    (["tool", "run", "moviepilot_api"], {"result": None}, "data.result"),
])
def test_tool_commands_reject_invalid_data(
    http_request: Mock, command: list[str], data: object, expected: str,
) -> None:
    """损坏的成功信封不得变成静默输出或错误的成功回显。"""
    http_request.return_value = _http_response(data)

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 1
    assert expected in result.output


def test_tool_show_preserves_missing_tool_error(http_request: Mock) -> None:
    """不存在的工具仍使用 CLI 原有的明确错误。"""
    http_request.return_value = {"status": 404, "json": {}, "text": ""}

    result = CliRunner().invoke(cli, ["tool", "show", "missing"])

    assert result.exit_code == 1
    assert "工具不存在：missing" in result.output


@pytest.mark.parametrize("command", [["scheduler", "list"], ["scheduler", "run", "job-1"]])
def test_scheduler_reports_inner_business_failure(http_request: Mock, command: list[str]) -> None:
    """REST 调用成功不掩盖网关返回的业务失败。"""
    http_request.return_value = _http_response({
        "result": json.dumps({"success": False, "message": "任务不存在", "data": None}),
    })

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 1
    assert "任务不存在" in result.output


@pytest.mark.parametrize("data", [None, ""])
def test_scheduler_run_reports_empty_success(http_request: Mock, data: object) -> None:
    """无业务返回值时仍显示调度调用成功，并且只调用一次。"""
    http_request.return_value = _http_response({
        "result": json.dumps({"success": True, "message": "", "data": data}),
    })

    result = CliRunner().invoke(cli, ["scheduler", "run", "job-1"])

    assert result.exit_code == 0
    assert result.output == "调度任务调用成功（无返回内容）\n"
    http_request.assert_called_once()


def test_tool_list_accepts_empty_catalog(http_request: Mock) -> None:
    """合法空工具目录不应误判为请求失败。"""
    http_request.return_value = _http_response([])

    result = CliRunner().invoke(cli, ["tool", "list"])

    assert result.exit_code == 0
    assert result.output == ""
