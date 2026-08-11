import asyncio
from statistics import median
from time import perf_counter
from unittest.mock import MagicMock, patch

import pytest
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agent.policy import sanitize_for_host, summarize_error, summarize_input, summarize_result
from app.agent.tools.base import MoviePilotTool, serialize_tool_result_for_agent
from app.agent.tools.manager import MoviePilotToolsManager


SECRET_MARKER = "nested-secret-marker-8472"


class _SecretInput(BaseModel):
    """日志脱敏测试工具的输入契约。"""

    payload: dict = Field(description="包含嵌套值的测试载荷")


class _SecretResultTool(MoviePilotTool):
    """返回嵌套敏感值的测试工具。"""

    name: str = "secret_result_tool"
    description: str = "Return a nested secret test payload."
    args_schema: type[BaseModel] = _SecretInput

    async def run(self, payload: dict) -> dict:
        """返回输入载荷，验证 shadow 模式不改变工具结果。"""
        return {
            "ok": True,
            "nested": [payload, {"authorization": f"Bearer {SECRET_MARKER}"}],
        }


class _SecretErrorTool(_SecretResultTool):
    """抛出包含敏感值异常的测试工具。"""

    name: str = "secret_error_tool"

    async def run(self, payload: dict) -> dict:
        """抛出测试异常。"""
        raise RuntimeError(f"api_key={SECRET_MARKER}")


def _logged_text(mock_logger: MagicMock) -> str:
    """汇总 mock logger 收到的全部消息文本。"""
    calls = []
    for method_name in ("debug", "info", "warning", "error"):
        method = getattr(mock_logger, method_name)
        calls.extend(str(call) for call in method.call_args_list)
    return "\n".join(calls)


def test_recursive_sanitizer_redacts_nested_structures_and_json_text() -> None:
    """嵌套 mapping/sequence 与 JSON 字符串都不能保留 secret marker。"""
    payload = {
        "name": "normal-name",
        "items": [
            {"api_key": SECRET_MARKER},
            {"headers": {"Authorization": f"Bearer {SECRET_MARKER}"}},
            '{"cookie":"' + SECRET_MARKER + '","count":2}',
        ],
        "token_count": 12,
    }

    sanitized = sanitize_for_host(payload)
    serialized = str(sanitized)

    assert SECRET_MARKER not in serialized
    assert "normal-name" in serialized
    assert sanitized["token_count"] == 12
    assert "***" in serialized


@pytest.mark.parametrize(
    "field_name",
    [
        "accessToken",
        "refreshToken",
        "apiKey",
        "APIKey",
        "apikey",
        "APIKEY",
        "authToken",
        "clientSecret",
        "appSecret",
        "proxyAuthorization",
        "dbPwd",
        "passKey",
        "secretKey",
        "SECRETKEY",
        "AccessKeySecret",
        "awsSecretAccessKey",
    ],
)
def test_recursive_sanitizer_redacts_camel_case_secret_fields(
    field_name: str,
) -> None:
    """结构化第三方载荷的驼峰凭据字段必须脱敏，统计字段保持可见。"""
    payload = {
        field_name: SECRET_MARKER,
        "tokenCount": 12,
    }

    sanitized = sanitize_for_host(payload)

    assert sanitized[field_name] == "***"
    assert sanitized["tokenCount"] == 12
    assert SECRET_MARKER not in str(sanitized)


@pytest.mark.parametrize(
    ("source", "secret_parts"),
    [
        (
            'password="quoted-secret-alpha quoted-secret-beta"',
            ("quoted-secret-alpha", "quoted-secret-beta"),
        ),
        (
            "password='single-secret-alpha single-secret-beta'",
            ("single-secret-alpha", "single-secret-beta"),
        ),
        (
            f"DATABASE_PASSWORD={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"OPENAI_API_KEY={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"MOVIEPILOT_API_TOKEN={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"authToken={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"dbPassword={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"secretKey={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"proxyAuthorization={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"awsSecretAccessKey={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"passKey={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"url=https://example.invalid/callback?authToken={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            f"{'x' * 300}AuthToken={SECRET_MARKER}",
            (SECRET_MARKER,),
        ),
        (
            "password=unquoted-secret-alpha unquoted-secret-beta; status=failed",
            ("unquoted-secret-alpha", "unquoted-secret-beta"),
        ),
        (
            "DATABASE_PASSWORD=correct horse battery staple, retry=off",
            ("correct", "horse", "battery", "staple"),
        ),
        (
            f"DATABASE_PASSWORD={SECRET_MARKER}#password-tail&more, retry=off",
            (SECRET_MARKER, "password-tail", "more"),
        ),
    ],
)
def test_sanitizer_redacts_secret_assignments(
    source: str,
    secret_parts: tuple[str, ...],
) -> None:
    """常见字段拼写、业务前缀和多词凭据都必须完整脱敏。"""
    sanitized = str(sanitize_for_host(source))

    assert "***" in sanitized
    for secret_part in secret_parts:
        assert secret_part not in sanitized

    if "; status=failed" in source:
        assert "; status=failed" in sanitized
    if ", retry=off" in source:
        assert ", retry=off" in sanitized


@pytest.mark.parametrize(
    "source",
    [
        "tokenCount=12",
        "tokenType=usage",
        "secretVersion=2",
        "apiKeyId=public-id",
        "accessTokenExpiresAt=2030-01-01T00:00:00Z",
        "passwordHash=sha256:diagnostic",
        "url=https://example.invalid/callback?apiKeyId=public-id",
    ],
)
def test_sanitizer_preserves_metadata_assignments(source: str) -> None:
    """带凭据词根但以 metadata 语义结尾的字段保持诊断价值。"""
    assert sanitize_for_host(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f"url=https://example.invalid/cb?authToken={SECRET_MARKER}&status=ok#done",
            "url=https://example.invalid/cb?authToken=***&status=ok#done",
        ),
        (
            f"url=https://example.invalid/cb?authToken={SECRET_MARKER}"
            f"&refreshToken={SECRET_MARKER}#done",
            "url=https://example.invalid/cb?authToken=***&refreshToken=***#done",
        ),
        (
            f'message="authToken={SECRET_MARKER}"; status=failed',
            'message="authToken=***"; status=failed',
        ),
        (
            'message="authToken="; status=ok',
            'message="authToken=***"; status=ok',
        ),
        (
            "message='authToken='; status=ok",
            "message='authToken=***'; status=ok",
        ),
        (
            'message="prefix authToken="; status=ok',
            'message="prefix authToken=***"; status=ok',
        ),
        (
            'authToken=""',
            'authToken=***',
        ),
        (
            'url=https://example.invalid/cb?authToken=&status=ok',
            'url=https://example.invalid/cb?authToken=***&status=ok',
        ),
        (
            'authToken="unterminated',
            'authToken=***',
        ),
    ],
)
def test_sanitizer_preserves_nested_assignment_boundaries(
    source: str,
    expected: str,
) -> None:
    """内层凭据脱敏后保留 URL 分段与外层引号结构。"""
    assert sanitize_for_host(source) == expected


@pytest.mark.parametrize("unit", ["a=", "a."])
def test_sanitizer_assignment_scan_scales_at_text_limit(unit: str) -> None:
    """赋值链和无头字段链在宿主文本上限内保持近似线性扫描。"""

    def median_duration(size: int) -> float:
        source = (unit * (size // len(unit) + 1))[:size]
        durations = []
        for _ in range(3):
            started_at = perf_counter()
            assert sanitize_for_host(source) == source
            durations.append(perf_counter() - started_at)
        return median(durations)

    small_duration = median_duration(4 * 1024)
    max_duration = median_duration(16 * 1024)

    # 4x 输入允许 10x 时间与 20ms 调度余量，同时约束同步宿主观测的延迟增长。
    assert max_duration <= small_duration * 10 + 0.02


def test_camel_case_secret_assignment_is_redacted_from_host_summaries() -> None:
    """输入、结果和异常摘要共享非结构化凭据赋值的脱敏契约。"""
    source = f"authToken={SECRET_MARKER}"

    summaries = (
        summarize_input(source),
        summarize_result(source),
        summarize_error(RuntimeError(source)),
    )

    assert all(SECRET_MARKER not in summary for summary in summaries)
    assert all("***" in summary for summary in summaries)


def test_sanitizer_redacts_unquoted_multiword_secret_in_error_summary() -> None:
    """异常中的无引号多词凭据必须净化到可靠分隔符。"""
    summary = summarize_error(
        RuntimeError("password=alpha beta; operation=connect")
    )

    assert "alpha" not in summary
    assert "beta" not in summary
    assert "operation=connect" in summary


def test_sanitizer_type_fallback_ignores_hostile_metaclass() -> None:
    """对象协议与类型名读取同时失败时仍应返回稳定占位。"""
    secret_marker = "hostile-type-secret-4381"

    class _HostileMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError(f"DATABASE_PASSWORD={secret_marker}")
            return super().__getattribute__(name)

    class _HostileValue(metaclass=_HostileMeta):
        def __str__(self) -> str:
            raise RuntimeError("string conversion failed")

    escaped = False
    try:
        sanitized = sanitize_for_host(_HostileValue())
    except BaseException:
        escaped = True
        sanitized = ""

    assert escaped is False
    assert str(sanitized).startswith("<unavailable:")
    assert secret_marker not in str(sanitized)


def test_sanitizer_bounds_json_shaped_text_before_parsing() -> None:
    """超过文本上限的 JSON 外形输入不得触发完整解析。"""
    secret_marker = "oversized-json-secret-9056"
    source = (
        '{"password":"' + secret_marker + '","padding":"' + "x" * 20000 + '"}'
    )

    with patch(
        "app.agent.policy.sanitizer.json.loads",
        side_effect=AssertionError("oversized JSON must not be parsed"),
    ) as mock_loads:
        sanitized = str(sanitize_for_host(source))

    mock_loads.assert_not_called()
    assert secret_marker not in sanitized
    assert sanitized.endswith("<truncated>")
    assert len(sanitized) < 17000


def test_sanitizer_handles_cyclic_command_without_raising() -> None:
    """循环 LangGraph Command 必须生成有界摘要而不是破坏工具成功结果。"""
    cycle = []
    command = Command(update={"state": cycle})
    cycle.append(command)

    summary = summarize_result(command, max_chars=240)

    assert len(summary) <= 240
    assert summary


def test_tool_result_json_fallback_warning_is_sanitized() -> None:
    """结果序列化 fallback 不能把第三方异常中的凭据写入 warning。"""

    class _FallbackResult:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(f"DATABASE_PASSWORD={SECRET_MARKER}")
            return "fallback-result"

    mock_logger = MagicMock()
    with patch("app.agent.tools.base.logger", mock_logger):
        result = serialize_tool_result_for_agent(_FallbackResult())

    assert result == "fallback-result"
    logged = _logged_text(mock_logger)
    assert SECRET_MARKER not in logged
    assert "RuntimeError" in logged


def test_summary_helpers_bound_output_without_losing_normal_context() -> None:
    """输入、结果与异常摘要应有界且保留非敏感诊断上下文。"""
    payload = {
        "query": "MoviePilot",
        "password": SECRET_MARKER,
        "body": "x" * 2000,
    }

    input_summary = summarize_input(payload, max_chars=240)
    result_summary = summarize_result(payload, max_chars=240)
    error_summary = summarize_error(
        RuntimeError(f"Authorization: Bearer {SECRET_MARKER}"),
        max_chars=240,
    )

    for summary in (input_summary, result_summary, error_summary):
        assert len(summary) <= 240
        assert SECRET_MARKER not in summary
    assert "MoviePilot" in input_summary


def test_agent_tool_logs_are_sanitized_but_shadow_result_is_unchanged() -> None:
    """G1 只净化宿主日志，shadow 工具返回值仍保持兼容。"""
    tool = _SecretResultTool(session_id="session-1", user_id="user-1")
    payload = {"token": SECRET_MARKER, "label": "visible"}
    mock_logger = MagicMock()

    with patch("app.agent.tools.base.logger", mock_logger):
        result = asyncio.run(tool._arun(payload=payload))

    assert SECRET_MARKER in result
    logged = _logged_text(mock_logger)
    assert SECRET_MARKER not in logged
    assert "visible" in logged


def test_direct_manager_logs_are_sanitized_but_result_is_unchanged() -> None:
    """HTTP/MCP/CLI manager 与 Agent 路径使用同一 secret-safe 日志语义。"""
    tool = _SecretResultTool(session_id="session-1", user_id="user-1")
    manager = MoviePilotToolsManager(is_admin=True)
    manager.tools = [tool]
    payload = {"cookie": SECRET_MARKER, "label": "visible"}
    mock_logger = MagicMock()

    with (
        patch("app.agent.tools.manager.logger", mock_logger),
        patch("app.agent.policy.orchestrator.logger", mock_logger),
    ):
        result = asyncio.run(manager.call_tool(tool.name, {"payload": payload}))

    assert SECRET_MARKER in result
    logged = _logged_text(mock_logger)
    assert SECRET_MARKER not in logged
    assert "visible" in logged


def test_tool_error_does_not_echo_secret_to_logs_or_result() -> None:
    """异常消息中的凭据既不能进日志，也不能回显给模型或 direct 调用方。"""
    tool = _SecretErrorTool(session_id="session-1", user_id="user-1")
    payload = {"token": SECRET_MARKER}
    mock_logger = MagicMock()

    with patch("app.agent.tools.base.logger", mock_logger):
        result = asyncio.run(tool._arun(payload=payload))

    assert SECRET_MARKER not in result
    assert SECRET_MARKER not in _logged_text(mock_logger)
    assert "***" in result
