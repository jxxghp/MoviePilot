import asyncio
from statistics import median
from time import perf_counter
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

import app.agent.policy.sanitizer as sanitizer_module
from app.agent.policy import sanitize_for_host, summarize_error, summarize_input, summarize_result
from app.agent.tools.base import MoviePilotTool, serialize_tool_result_for_agent
from app.agent.tools.manager import MoviePilotToolsManager


SECRET_MARKER = "nested-secret-marker-8472"


class _SecretInput(BaseModel):
    """日志脱敏测试工具的输入契约。"""

    payload: dict = Field(description="包含嵌套值的测试载荷")


class _InvalidSecretInput(BaseModel):
    """用于验证 Pydantic 输入错误不会回显凭据原值。"""

    api_key: int


class _DynamicSecretLocationInput(BaseModel):
    """用于验证动态 Mapping key 不会通过错误位置泄漏。"""

    payload: dict[str, int]


class _CustomSecretValidationInput(BaseModel):
    """用于验证自定义错误类型、消息和上下文不会进入宿主摘要。"""

    value: str

    @field_validator("value")
    @classmethod
    def reject_value(cls, value: str) -> str:
        """构造携带输入值的第三方自定义校验错误。"""
        raise PydanticCustomError(
            f"custom_{value}",
            f"rejected {value}",
            {"rejected_value": value},
        )


class _NamedTupleCredential(NamedTuple):
    """模拟插件或第三方 SDK 返回的命名元组。"""

    api_key: str
    label: str


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


def test_recursive_sanitizer_redacts_named_tuple_secret_fields() -> None:
    """命名元组必须保留字段语义并按字段名脱敏。"""
    payload = _NamedTupleCredential(
        api_key=SECRET_MARKER,
        label="visible-label",
    )

    sanitized = sanitize_for_host(payload)

    assert sanitized == {"api_key": "***", "label": "visible-label"}
    assert SECRET_MARKER not in str(sanitized)


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f"database_url=postgresql://alice:{SECRET_MARKER}"
            "@example.invalid/media",
            "database_url=postgresql://***@example.invalid/media",
        ),
        (
            f"endpoint=https://{SECRET_MARKER}@example.invalid/path",
            "endpoint=https://***@example.invalid/path",
        ),
        (
            f"dsn=postgresql://alice:{SECRET_MARKER}%40tail"
            "@[2001:db8::1]:5432/media?sslmode=require",
            "dsn=postgresql://***@[2001:db8::1]:5432/media?sslmode=require",
        ),
        (
            f"primary=https://alice:{SECRET_MARKER}@one.invalid/a "
            f"secondary=redis://:{SECRET_MARKER}-two@two.invalid/0",
            "primary=https://***@one.invalid/a "
            "secondary=redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid,"
            f"redis://:{SECRET_MARKER}-two@two.invalid/0",
            "https://***@one.invalid,redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid;"
            f"redis://:{SECRET_MARKER}-two@two.invalid/0",
            "https://***@one.invalid;redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid|"
            f"redis://:{SECRET_MARKER}-two@two.invalid/0",
            "https://***@one.invalid|redis://***@two.invalid/0",
        ),
    ],
)
def test_sanitizer_redacts_uri_userinfo(source: str, expected: str) -> None:
    """URI authority 中的 userinfo 不得进入宿主摘要。"""
    assert sanitize_for_host(source) == expected


@pytest.mark.parametrize("escape_layers", [1, 2])
def test_sanitizer_redacts_slash_escaped_uri_userinfo(
    escape_layers: int,
) -> None:
    """嵌入诊断文本中的 slash-escaped URI 仍须清理 userinfo。"""
    separator = ":" + "\\" * escape_layers + "/" + "\\" * escape_layers + "/"
    source = (
        r'payload={\"dsn\":\"postgresql'
        f"{separator}alice:{SECRET_MARKER}@example.invalid/media"
        r'\"}'
    )

    sanitized = str(sanitize_for_host(source))

    assert SECRET_MARKER not in sanitized
    assert f"postgresql{separator}***@example.invalid/media" in sanitized


def test_sanitizer_preserves_slash_escaped_uri_without_userinfo() -> None:
    """slash-escaped URI 没有 userinfo 时保持原始诊断文本。"""
    source = r"url=https:\/\/example.invalid/path?email=user@example.invalid"

    assert sanitize_for_host(source) == source


def test_sanitizer_redacts_truncated_uri_with_unresolved_userinfo() -> None:
    """截断点前无法确认 authority 结束时按敏感内容处理。"""
    source = (
        f"dsn=postgresql://alice:{SECRET_MARKER}"
        f"{'x' * (16 * 1024)}@example.invalid/media"
    )

    summaries = (
        summarize_input(source),
        summarize_result(source),
        summarize_error(RuntimeError(source)),
    )

    assert all(SECRET_MARKER not in summary for summary in summaries)
    assert all("***" in summary for summary in summaries)
    assert all("<truncated>" in summary for summary in summaries)


def test_sanitizer_redacts_truncated_uri_after_early_at_sign() -> None:
    """截断 authority 内的早期 `@` 不能证明 userinfo 已完整结束。"""
    trailing_secret = "truncated-uri-tail-secret-5931"
    prefix = f"https://user:{SECRET_MARKER}@{trailing_secret}"
    source = (
        prefix
        + "x" * (16 * 1024 - len(prefix))
        + "@example.invalid/media"
    )

    summaries = (
        summarize_input(source),
        summarize_result(source),
        summarize_error(RuntimeError(source)),
    )

    assert all(SECRET_MARKER not in summary for summary in summaries)
    assert all(trailing_secret not in summary for summary in summaries)
    assert all("***" in summary for summary in summaries)
    assert all("<truncated>" in summary for summary in summaries)


@pytest.mark.parametrize(
    "source",
    [
        'payload="{\\"apiKey\\":\\"' + SECRET_MARKER + '\\"}"',
        rf'payload=\"{{\\\"apiKey\\\":\\\"{SECRET_MARKER}\\\"}}\"',
    ],
)
def test_sanitizer_redacts_escaped_json_secret_fields(source: str) -> None:
    """普通文本内多层转义的 JSON 凭据字段仍须脱敏。"""
    sanitized = str(sanitize_for_host(source))

    assert SECRET_MARKER not in sanitized
    assert "***" in sanitized
    assert "}" in sanitized


def test_sanitizer_preserves_tail_after_escaped_json_secret() -> None:
    """转义 JSON 凭据中的分隔符不应截断脱敏或吞掉后续字段。"""
    source = (
        'payload="{\\"apiKey\\":\\"'
        f"{SECRET_MARKER},still-secret"
        '\\",\\"status\\":\\"ok\\"}"'
    )

    sanitized = str(sanitize_for_host(source))

    assert SECRET_MARKER not in sanitized
    assert "still-secret" not in sanitized
    assert "status" in sanitized
    assert "ok" in sanitized


@pytest.mark.parametrize("escape_layers", [0, 1, 2])
def test_sanitizer_handles_trailing_backslashes_before_secret_quote(
    escape_layers: int,
) -> None:
    """凭据值末尾的 literal backslash 不得吞掉后续敏感字段。"""
    payload = (
        '{"authToken":"first-secret\\\\",'
        '"refreshToken":"second-secret","status":"ok"}'
    )
    for _ in range(escape_layers):
        escaped_payload = payload.replace("\\", "\\\\").replace('"', '\\"')
        payload = f'"{escaped_payload}"'
    source = f"payload={payload}"

    sanitized = str(sanitize_for_host(source))

    assert "first-secret" not in sanitized
    assert "second-secret" not in sanitized
    assert sanitized.count("***") == 2
    assert "status" in sanitized
    assert "ok" in sanitized


def test_sanitizer_preserves_escaped_json_metadata_fields() -> None:
    """转义 JSON 中的 metadata 字段保持诊断值。"""
    source = 'payload="{\\"apiKeyId\\":\\"public-id\\"}"'

    assert sanitize_for_host(source) == source


def test_sanitizer_preserves_uri_without_userinfo() -> None:
    """不含 userinfo 的 URL 及 query 邮箱保持原始诊断信息。"""
    source = "url=https://example.invalid/path?email=user@example.invalid"

    assert sanitize_for_host(source) == source


@pytest.mark.parametrize(
    "unit",
    ["a=", "a.", "a://host/", "\\", "\\\""],
)
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


def test_sanitizer_bounds_oversized_mapping_key_normalization() -> None:
    """超长结构化字段只允许固定窗口进入凭据名规范化。"""

    class _TrackingPattern:
        """记录正则收到的最大文本长度并复用真实匹配行为。"""

        def __init__(self, pattern) -> None:
            self.pattern = pattern
            self.max_chars = 0

        def sub(self, replacement: str, value: str) -> str:
            self.max_chars = max(self.max_chars, len(value))
            return self.pattern.sub(replacement, value)

    padding = "x" * (2 * 1024 * 1024)
    secret_pattern = _TrackingPattern(
        sanitizer_module._ACRONYM_BOUNDARY_PATTERN
    )
    camel_pattern = _TrackingPattern(
        sanitizer_module._CAMEL_CASE_BOUNDARY_PATTERN
    )
    payload = {
        f"secret-prefix-{padding}AuthToken": SECRET_MARKER,
        f"metadata-prefix-{padding}tokenCount": 12,
    }

    with (
        patch.object(
            sanitizer_module,
            "_ACRONYM_BOUNDARY_PATTERN",
            secret_pattern,
        ),
        patch.object(
            sanitizer_module,
            "_CAMEL_CASE_BOUNDARY_PATTERN",
            camel_pattern,
        ),
    ):
        sanitized = sanitize_for_host(payload)

    assert SECRET_MARKER not in str(sanitized)
    assert "***" in sanitized.values()
    assert 12 in sanitized.values()
    assert secret_pattern.max_chars <= 1024
    assert camel_pattern.max_chars <= 1024


@pytest.mark.parametrize(
    ("first_name", "second_name", "value", "expected"),
    [
        ("api_key", "label", SECRET_MARKER, "***"),
        ("tokenCount", "api_key", 12, 12),
    ],
)
def test_sanitizer_uses_one_snapshot_for_stateful_mapping_key(
    first_name: str,
    second_name: str,
    value: object,
    expected: object,
) -> None:
    """动态 Mapping key 的输出名和判敏必须复用同一次字符串快照。"""

    class _StatefulKey:
        """每次字符串化返回不同字段名的第三方 key。"""

        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return first_name if self.calls == 1 else second_name

    key = _StatefulKey()

    sanitized = sanitize_for_host({key: value})

    assert key.calls == 1
    assert sanitized == {first_name: expected}


def test_sanitizer_redacts_value_for_uninspectable_mapping_key() -> None:
    """无法取得稳定名称的 Mapping key 按敏感字段处理。"""

    class _UninspectableKey:
        """模拟字符串协议故障的第三方 key。"""

        def __str__(self) -> str:
            raise RuntimeError(f"unavailable key {SECRET_MARKER}")

    sanitized = sanitize_for_host({_UninspectableKey(): SECRET_MARKER})

    assert sanitized == {"<key:_UninspectableKey>": "***"}
    assert SECRET_MARKER not in str(sanitized)


def test_sanitizer_bounds_shared_reference_expansion() -> None:
    """整个净化调用共享工作预算，重复引用不能按分支指数展开。"""

    class _CountingValue:
        """记录共享叶节点被字符串化的次数。"""

        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "visible-leaf"

    leaf = _CountingValue()
    payload = leaf
    for _ in range(7):
        payload = [payload] * 5

    sanitized = sanitize_for_host(payload)

    assert leaf.calls <= 2000
    assert "<work-limit>" in str(sanitized)


def test_sanitizer_budget_bounds_container_item_expansion() -> None:
    """共享 DAG 的容器读取和输出项总量必须受全调用预算约束。"""

    class _CountingMapping(dict):
        """统计 Mapping iterator 实际交付给 sanitizer 的项数。"""

        yielded_items = 0

        def items(self):
            for item in super().items():
                type(self).yielded_items += 1
                yield item

    def count_entries(value: object) -> int:
        """统计 sanitizer 结果中实际生成的容器项数。"""
        if isinstance(value, dict):
            return len(value) + sum(count_entries(item) for item in value.values())
        if isinstance(value, list):
            return len(value) + sum(count_entries(item) for item in value)
        return 0

    shared: object = {"label": "visible"}
    for level in range(7):
        node = _CountingMapping(
            {
                f"field{level}_{index}ApiKey": SECRET_MARKER
                for index in range(97)
            }
        )
        node.update({f"child{index}": shared for index in range(3)})
        shared = node

    sanitized = sanitize_for_host(shared)
    max_emitted_items = sanitizer_module._MAX_WORK_ITEMS + 16

    assert _CountingMapping.yielded_items <= max_emitted_items
    assert count_entries(sanitized) <= max_emitted_items
    assert "<work-limit>" in str(sanitized)


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


def test_pydantic_validation_error_is_safe_across_host_entry_points() -> None:
    """Pydantic 原始输入不得从递归 sanitizer 或任一摘要入口回显。"""
    with pytest.raises(ValidationError) as exc_info:
        _InvalidSecretInput(api_key=SECRET_MARKER)

    error = exc_info.value
    sanitized = sanitize_for_host(error)
    outputs = (
        str(sanitized),
        str(sanitize_for_host({"error": error})),
        summarize_input(error),
        summarize_result({"error": error}),
        summarize_error(error),
    )

    assert all(SECRET_MARKER not in output for output in outputs)
    assert sanitized == {"error_count": 1}
    assert "ValidationError" in outputs[-1]


def test_pydantic_validation_error_excludes_dynamic_metadata() -> None:
    """动态错误位置、类型、消息和上下文均不得成为宿主诊断文本。"""
    with pytest.raises(ValidationError) as location_exc_info:
        _DynamicSecretLocationInput(
            payload={SECRET_MARKER: "not-an-integer"}
        )
    with pytest.raises(ValidationError) as custom_exc_info:
        _CustomSecretValidationInput(value=SECRET_MARKER)

    outputs = []
    for error in (location_exc_info.value, custom_exc_info.value):
        outputs.extend(
            (
                str(sanitize_for_host(error)),
                str(sanitize_for_host({"error": error})),
                summarize_result({"error": error}),
                summarize_error(error),
            )
        )

    assert all(SECRET_MARKER not in output for output in outputs)
    assert all("error_count" in output for output in outputs)


def test_pydantic_validation_error_count_does_not_expand_details() -> None:
    """校验错误计数不得构造完整 errors 明细。"""
    with pytest.raises(ValidationError) as exc_info:
        _InvalidSecretInput(api_key=SECRET_MARKER)

    with patch.object(
        ValidationError,
        "errors",
        side_effect=AssertionError("validation details must not be expanded"),
    ) as mock_errors:
        sanitized = sanitize_for_host(exc_info.value)

    mock_errors.assert_not_called()
    assert sanitized == {"error_count": 1}


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
