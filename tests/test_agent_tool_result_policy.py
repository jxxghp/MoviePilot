import asyncio
import base64
import json
from statistics import median
from time import perf_counter
from typing import Annotated, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from langgraph.types import Command
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    ValidationError,
    field_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic_core import PydanticCustomError

import app.agent.policy.sanitizer as sanitizer_module
# pylint: disable=no-name-in-module  # 策略包根通过 __getattr__ 惰性导出，Pylint 无法静态解析。
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


class _AliasedCredentialResult(BaseModel):
    """模拟外部凭据名与 Python 属性名不同的结构化结果。"""

    credential: str = Field(alias="apiKey")


class _ValidationAliasedCredentialResult(BaseModel):
    """模拟仅通过 validation alias 接受凭据的 Pydantic 字段。"""

    credential: str = Field(validation_alias="apiToken")


class _ChoiceAliasedCredentialResult(BaseModel):
    """模拟敏感名称位于后续 choice 的 Pydantic 字段。"""

    credential: str = Field(validation_alias=AliasChoices("credentialLabel", "apiKey"))


class _PathAliasedCredentialResult(BaseModel):
    """模拟通过带整数索引的嵌套路径接收凭据的 Pydantic 字段。"""

    credential: str = Field(validation_alias=AliasPath("payload", 0, "clientSecret"))


class _ChoicePathAliasedCredentialResult(BaseModel):
    """模拟后续 choice 使用嵌套路径的 Pydantic 凭据字段。"""

    credential: str = Field(
        validation_alias=AliasChoices(
            "credentialLabel",
            AliasPath("payload", "refreshToken"),
        )
    )


class _SerializationAliasedCredentialResult(BaseModel):
    """模拟仅在序列化契约中使用凭据名称的结构化结果。"""

    credential: str = Field(serialization_alias="clientSecret")


class _AliasedMetadataResult(BaseModel):
    """模拟外部 metadata 名称与 Python 属性名不同的结构化结果。"""

    count: int = Field(alias="tokenCount")


class _DisguisedSecretAlias(str):
    """保存敏感底层值但通过字符串协议伪装成 metadata 名称。"""

    def __str__(self) -> str:
        return "tokenCount"


class _HostileAliasedCredentialResult(BaseModel):
    """模拟使用 hostile str 子类作为直接别名的 Pydantic 字段。"""

    credential: str = Field(alias=_DisguisedSecretAlias("apiKey"))


class _HostilePathAliasedCredentialResult(BaseModel):
    """模拟 AliasPath 中包含 hostile str 子类的 Pydantic 字段。"""

    credential: str = Field(
        validation_alias=AliasPath(
            "payload",
            _DisguisedSecretAlias("clientSecret"),
        )
    )


@pydantic_dataclass
class _AliasedCredentialDataclass:
    """模拟通过赋值形式声明外部凭据名的 Pydantic dataclass。"""

    credential: str = Field(alias="apiKey")


@pydantic_dataclass
class _AnnotatedAliasedCredentialDataclass:
    """模拟通过 Annotated 声明外部凭据名的 Pydantic dataclass。"""

    credential: Annotated[str, Field(alias="refreshToken")]


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


@pytest.mark.parametrize("as_json_text", [False, True])
def test_recursive_sanitizer_redacts_values_identified_by_secret_setting_key(
    as_json_text: bool,
) -> None:
    """设置项身份为凭据时，同一结构中的通用值字段也必须脱敏。"""
    payload = {
        "settings": [
            {
                "setting_key": "API_TOKEN",
                "value": SECRET_MARKER,
                "value_preview": SECRET_MARKER,
                "metadata": {"value": "visible-nested-value"},
            }
        ],
        "value": "visible-outer-value",
    }
    source = json.dumps(payload, ensure_ascii=False) if as_json_text else payload

    sanitized = sanitize_for_host(source)
    if as_json_text:
        sanitized = json.loads(sanitized)

    setting = sanitized["settings"][0]
    assert setting["value"] == "***"
    assert setting["value_preview"] == "***"
    assert setting["metadata"]["value"] == "visible-nested-value"
    assert sanitized["value"] == "visible-outer-value"
    assert SECRET_MARKER not in json.dumps(sanitized, ensure_ascii=False)


def test_recursive_sanitizer_preserves_values_for_nonsecret_setting_key() -> None:
    """普通设置的 value 字段仍应保留可诊断内容。"""
    payload = {
        "setting_key": "PROJECT_NAME",
        "value": "MoviePilot",
        "value_preview": "MoviePilot",
    }

    sanitized = sanitize_for_host(payload)

    assert sanitized == payload


@pytest.mark.parametrize(
    "setting_key",
    [
        "API_TOKEN",
        "LLM_API_KEY",
        "COOKIECLOUD_KEY",
        "COOKIECLOUD_AUTH_HEADER",
        "SUPERUSER_PASSWORD",
        "DB_POSTGRESQL_PASSWORD",
        "GITHUB_TOKEN",
        "FEISHU_VERIFICATION_TOKEN",
        "SECRET_KEY",
        "RESOURCE_SECRET_KEY",
    ],
)
def test_recursive_sanitizer_redacts_shared_secret_setting_identities(
    setting_key: str,
) -> None:
    """宿主回执必须与系统设置工具共享敏感设置身份语义。"""
    payload = {
        "setting_key": setting_key,
        "value": SECRET_MARKER,
        "value_preview": SECRET_MARKER,
    }

    sanitized = sanitize_for_host(payload)

    assert sanitized["value"] == "***"
    assert sanitized["value_preview"] == "***"


@pytest.mark.parametrize(
    "setting_key",
    [
        "PROJECT_NAME",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "LLM_MAX_CONTEXT_TOKENS",
        "COOKIECLOUD_INTERVAL",
    ],
)
def test_recursive_sanitizer_preserves_shared_nonsecret_setting_identities(
    setting_key: str,
) -> None:
    """名称中提及凭据概念的普通设置仍应保留诊断值。"""
    payload = {
        "setting_key": setting_key,
        "value": "visible-value",
        "value_preview": "visible-value",
    }

    assert sanitize_for_host(payload) == payload


def test_recursive_sanitizer_fails_closed_when_setting_identity_is_truncated() -> None:
    """设置身份扫描不完整时，已捕获的通用值字段不能按明文放行。"""
    payload = {"value": SECRET_MARKER}
    payload.update({f"padding_{index}": index for index in range(100)})
    payload["setting_key"] = "API_TOKEN"

    sanitized = sanitize_for_host(payload)

    assert sanitized["value"] == "***"
    assert sanitized["<truncated>"] == "more items"
    assert SECRET_MARKER not in json.dumps(sanitized, ensure_ascii=False)


@pytest.mark.parametrize(
    "field_name",
    [
        "auth",
        "basicAuth",
        "authentication",
        "httpAuthentication",
        "credential",
        "credentials",
        "serviceCredentials",
    ],
)
def test_recursive_sanitizer_redacts_credential_containers(
    field_name: str,
) -> None:
    """认证与凭据容器必须在读取内部用户名或密码前整体遮蔽。"""
    payload = {
        field_name: ("alice", SECRET_MARKER),
        "authEnabled": True,
        "credentialCount": 1,
    }

    sanitized = sanitize_for_host(payload)

    assert sanitized == {
        field_name: "***",
        "authEnabled": True,
        "credentialCount": 1,
    }
    assert SECRET_MARKER not in str(sanitized)


@pytest.mark.parametrize("field_name", ["oauth", "OAuth", "oauth2", "OAuth2"])
def test_recursive_sanitizer_redacts_oauth_credential_containers(
    field_name: str,
) -> None:
    """OAuth 容器判敏必须与字段大小写及数字分词无关。"""
    payload = {
        field_name: ("alice", SECRET_MARKER),
        "oauthEnabled": True,
        "OAuthVersion": 2,
    }

    sanitized = sanitize_for_host(payload)

    assert sanitized == {
        field_name: "***",
        "oauthEnabled": True,
        "OAuthVersion": 2,
    }
    assert SECRET_MARKER not in str(sanitized)


def test_recursive_sanitizer_redacts_named_tuple_secret_fields() -> None:
    """命名元组必须保留字段语义并按字段名脱敏。"""
    payload = _NamedTupleCredential(
        api_key=SECRET_MARKER,
        label="visible-label",
    )

    sanitized = sanitize_for_host(payload)

    assert sanitized == {"api_key": "***", "label": "visible-label"}
    assert SECRET_MARKER not in str(sanitized)


def test_sanitizer_rejects_hostile_named_tuple_metadata_without_protocols() -> None:
    """伪造的 `_fields` 与 tuple 覆盖协议不得参与 named-tuple 分类。"""
    calls = []

    class _HostileFields(tuple):
        def __len__(self) -> int:
            calls.append("fields.__len__")
            raise AssertionError("hostile fields length executed")

        def __getitem__(self, index):
            calls.append("fields.__getitem__")
            raise AssertionError("hostile fields item executed")

    class _TupleLike(tuple):
        _fields = _HostileFields(("label",))

        def __len__(self) -> int:
            calls.append("value.__len__")
            raise AssertionError("hostile value length executed")

        def __getitem__(self, index):
            calls.append("value.__getitem__")
            raise AssertionError("hostile value item executed")

    sanitized = sanitize_for_host(_TupleLike(("visible",)))

    assert calls == []
    assert sanitized == ["visible"]


@pytest.mark.parametrize(
    "value",
    [
        _AliasedCredentialResult(apiKey=SECRET_MARKER),
        _ValidationAliasedCredentialResult(apiToken=SECRET_MARKER),
        _ChoiceAliasedCredentialResult(apiKey=SECRET_MARKER),
        _PathAliasedCredentialResult(payload=[{"clientSecret": SECRET_MARKER}]),
        _ChoicePathAliasedCredentialResult(payload={"refreshToken": SECRET_MARKER}),
        _SerializationAliasedCredentialResult(credential=SECRET_MARKER),
        _HostileAliasedCredentialResult.model_validate({"apiKey": SECRET_MARKER}),
        _HostilePathAliasedCredentialResult.model_validate({"payload": {"clientSecret": SECRET_MARKER}}),
    ],
)
def test_recursive_sanitizer_redacts_pydantic_secret_aliases(
    value: BaseModel,
) -> None:
    """Pydantic 字段的输入、路径及输出别名均参与凭据判定。"""
    sanitized = sanitize_for_host(value)

    assert sanitized == {"credential": "***"}
    assert SECRET_MARKER not in str(sanitized)


def test_recursive_sanitizer_preserves_pydantic_metadata_alias() -> None:
    """非敏感 Pydantic 外部别名不应遮蔽 metadata 值。"""
    assert sanitize_for_host(_AliasedMetadataResult(tokenCount=12)) == {"count": 12}


@pytest.mark.parametrize(
    "value",
    [
        _AliasedCredentialDataclass(apiKey=SECRET_MARKER),
        _AnnotatedAliasedCredentialDataclass(refreshToken=SECRET_MARKER),
    ],
)
def test_recursive_sanitizer_redacts_pydantic_dataclass_secret_aliases(
    value: object,
) -> None:
    """Pydantic dataclass 的解析后别名元数据同样参与凭据判定。"""
    sanitized = sanitize_for_host(value)

    assert sanitized == {"credential": "***"}
    assert SECRET_MARKER not in str(sanitized)


def test_pydantic_alias_path_limit_applies_before_iteration() -> None:
    """AliasPath 必须先验证长度边界，再读取或复制任何 path part。"""

    class _TrackingPath(list):
        """记录 alias path 实际向 sanitizer 交付的 part 数量。"""

        yielded_parts = 0

        def __iter__(self):
            for part in super().__iter__():
                type(self).yielded_parts += 1
                yield part

    alias = AliasPath("placeholder")
    alias.path = _TrackingPath(["metadata"] * (sanitizer_module._MAX_ITEMS + 1))

    assert sanitizer_module._pydantic_alias_names(alias) is None
    assert _TrackingPath.yielded_parts == 0


def test_pydantic_alias_choices_share_one_part_budget() -> None:
    """AliasPath 与普通 choice 共用额度，耗尽后必须 fail-closed。"""
    alias = AliasChoices(
        AliasPath(*(["metadata"] * sanitizer_module._MAX_ITEMS)),
        "metadataTail",
    )
    budget = [sanitizer_module._MAX_ITEMS]

    assert (
        sanitizer_module._pydantic_alias_names(
            alias,
            _budget=budget,
        )
        is None
    )
    assert budget == [0]

    class _OversizedAliasResult(BaseModel):
        """模拟 alias part 总量超过宿主固定额度的第三方模型。"""

        credential: str = Field(validation_alias=alias)

    sanitized = sanitize_for_host(_OversizedAliasResult.model_construct(credential=SECRET_MARKER))

    assert sanitized == {"credential": "***"}
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
    "field_name",
    ["api key", "access token", "client secret", "refresh token"],
)
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("escape_layers", range(4))
def test_sanitizer_redacts_escaped_quoted_secret_keys_with_spaces(
    field_name: str,
    quote: str,
    escape_layers: int,
) -> None:
    """转义 JSON 片段中的空格分隔凭据名必须复用结构化判敏语义。"""
    wrapper = "\\" * escape_layers + quote
    source = f"payload={{{wrapper}{field_name}{wrapper}:{wrapper}{SECRET_MARKER}{wrapper}}}"

    sanitized = str(sanitize_for_host(source))

    assert SECRET_MARKER not in sanitized
    assert "***" in sanitized
    assert field_name in sanitized


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("escape_layers", range(7))
@pytest.mark.parametrize("leading_whitespace", [" ", "\t", " \t"])
def test_sanitizer_redacts_quoted_secret_keys_with_leading_whitespace(
    quote: str,
    escape_layers: int,
    leading_whitespace: str,
) -> None:
    """quoted key 的前导横向空白不得绕过凭据名识别。"""
    wrapper = "\\" * escape_layers + quote
    source = f"payload={{{wrapper}{leading_whitespace}api key{wrapper}:{wrapper}{SECRET_MARKER}{wrapper}}}"

    outputs = (
        str(sanitize_for_host(source)),
        summarize_input(source),
        summarize_result(source),
        summarize_error(RuntimeError(source)),
    )

    assert all(SECRET_MARKER not in output for output in outputs)
    assert all("***" in output for output in outputs)


def test_sanitizer_preserves_escaped_quoted_metadata_key_with_spaces() -> None:
    """空格分隔的 metadata key 不应因 quoted-key 支持而被误判。"""
    source = r"payload=\"{\\\"token count\\\":12}\""

    assert sanitize_for_host(source) == source


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
def test_sanitizer_redacts_basic_auth_in_builtin_tuple_key(
    header: str,
) -> None:
    """内建 tuple key 中的 Basic Auth 与 URI userinfo 都不得进入输出 key。"""
    basic_token = "YWxpY2U6c3ludGhldGljLXBhc3N3b3Jk"
    payload = {
        (
            header,
            f"Basic {basic_token} https://alice:{SECRET_MARKER}@example.invalid",
        ): "ok"
    }

    sanitized = sanitize_for_host(payload)
    output_key = next(iter(sanitized))

    assert sanitized[output_key] == "ok"
    assert basic_token not in output_key
    assert SECRET_MARKER not in output_key
    assert "Basic ***" in output_key
    assert "https://***@example.invalid" in output_key


@pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC"])
@pytest.mark.parametrize(
    "basic_token",
    [
        "dTpw",
        "YWxpY2U6cA==",
        "YWxpY2U6c3ludGhldGljLXBhc3N3b3Jk",
    ],
)
def test_sanitizer_redacts_basic_auth_across_host_summaries(
    scheme: str,
    basic_token: str,
) -> None:
    """裸 Basic Auth token 在全部宿主摘要入口复用中央文本脱敏。"""
    source = f"upstream returned {scheme} {basic_token}. status=failed"

    outputs = (
        str(sanitize_for_host(source)),
        summarize_input(source),
        summarize_result(source),
        summarize_error(RuntimeError(source)),
    )

    assert all(basic_token not in output for output in outputs)
    assert all(f"{scheme} ***" in output for output in outputs)
    assert all("status=failed" in output for output in outputs)


@pytest.mark.parametrize(
    "source",
    [
        "transport uses basic mode",
        "scheme=Basic dG9rZW4=",
    ],
)
def test_sanitizer_preserves_noncredential_basic_metadata(source: str) -> None:
    """普通 basic 文案及不含 user:password 的 Base64 metadata 保持可见。"""
    assert sanitize_for_host(source) == source


def test_sanitizer_fails_closed_for_truncated_basic_auth_token() -> None:
    """Basic token 在文本上限内未闭合时遮蔽整个已保留前缀。"""
    prefix = "log: Basic "
    token = base64.b64encode(b"alice:" + b"x" * sanitizer_module._MAX_TEXT_CHARS).decode()
    source = prefix + token

    sanitized = str(sanitize_for_host(source))

    assert sanitized == f"{prefix}***<truncated>"
    assert token[:100] not in sanitized


def test_sanitizer_fails_closed_for_truncated_basic_auth_in_tuple_key() -> None:
    """tuple renderer 的内部截断事实必须传给 Basic token 脱敏。"""
    token = base64.b64encode(b"alice:" + b"x" * sanitizer_module._MAX_TEXT_CHARS).decode()
    payload = {("Authorization", f"Basic {token}"): "ok"}

    sanitized = sanitize_for_host(payload)
    output_key = next(iter(sanitized))

    assert sanitized[output_key] == "ok"
    assert "Basic ***" in output_key
    assert "<truncated>" in output_key
    assert token[:100] not in output_key


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f"url=https://example.invalid/cb?authToken={SECRET_MARKER}&status=ok#done",
            "url=https://example.invalid/cb?authToken=***&status=ok#done",
        ),
        (
            f"url=https://example.invalid/cb?authToken={SECRET_MARKER}&refreshToken={SECRET_MARKER}#done",
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
            "authToken=***",
        ),
        (
            "url=https://example.invalid/cb?authToken=&status=ok",
            "url=https://example.invalid/cb?authToken=***&status=ok",
        ),
        (
            'authToken="unterminated',
            "authToken=***",
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
            f"database_url=postgresql://alice:{SECRET_MARKER}@example.invalid/media",
            "database_url=postgresql://***@example.invalid/media",
        ),
        (
            f"endpoint=https://{SECRET_MARKER}@example.invalid/path",
            "endpoint=https://***@example.invalid/path",
        ),
        (
            f"dsn=postgresql://alice:{SECRET_MARKER}%40tail@[2001:db8::1]:5432/media?sslmode=require",
            "dsn=postgresql://***@[2001:db8::1]:5432/media?sslmode=require",
        ),
        (
            f"primary=https://alice:{SECRET_MARKER}@one.invalid/a secondary=redis://:{SECRET_MARKER}-two@two.invalid/0",
            "primary=https://***@one.invalid/a secondary=redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid,redis://:{SECRET_MARKER}-two@two.invalid/0",
            "https://***@one.invalid,redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid;redis://:{SECRET_MARKER}-two@two.invalid/0",
            "https://***@one.invalid;redis://***@two.invalid/0",
        ),
        (
            f"https://alice:{SECRET_MARKER}@one.invalid|redis://:{SECRET_MARKER}-two@two.invalid/0",
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
        r"payload={\"dsn\":\"postgresql"
        f"{separator}alice:{SECRET_MARKER}@example.invalid/media"
        r"\"}"
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
    source = f"dsn=postgresql://alice:{SECRET_MARKER}{'x' * (16 * 1024)}@example.invalid/media"

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
    source = prefix + "x" * (16 * 1024 - len(prefix)) + "@example.invalid/media"

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
        rf"payload=\"{{\\\"apiKey\\\":\\\"{SECRET_MARKER}\\\"}}\"",
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
    source = f'payload="{{\\"apiKey\\":\\"{SECRET_MARKER},still-secret\\",\\"status\\":\\"ok\\"}}"'

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
    payload = '{"authToken":"first-secret\\\\","refreshToken":"second-secret","status":"ok"}'
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
    ["a=", "a.", "a://host/", "\\", '\\"'],
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


def test_secret_assignment_slash_run_scales_at_text_limit() -> None:
    """凭据值中的连续反斜杠必须单向扫描，不能重复遍历同一后缀。"""

    def median_duration(slash_count: int) -> float:
        source = "password=" + "\\" * slash_count + "tail"
        durations = []
        for _ in range(3):
            started_at = perf_counter()
            sanitized = str(sanitize_for_host(source))
            durations.append(perf_counter() - started_at)
            assert sanitized.startswith("password=***")
        return median(durations)

    small_duration = median_duration(4 * 1024)
    max_duration = median_duration(sanitizer_module._MAX_TEXT_CHARS)

    # 4x 输入允许 10x 时间与 50ms 调度余量，同时排除平方级同步扫描。
    assert max_duration <= small_duration * 10 + 0.05


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
    secret_pattern = _TrackingPattern(sanitizer_module._ACRONYM_BOUNDARY_PATTERN)
    camel_pattern = _TrackingPattern(sanitizer_module._CAMEL_CASE_BOUNDARY_PATTERN)
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


@pytest.mark.parametrize("value", [SECRET_MARKER, 12])
def test_sanitizer_does_not_stringify_dynamic_mapping_key(
    value: object,
) -> None:
    """动态 Mapping key 不执行字符串协议，值按未知字段保守遮蔽。"""

    class _StatefulKey:
        """通过字符串协议伪装字段语义的第三方 key。"""

        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "api_key"

    key = _StatefulKey()

    sanitized = sanitize_for_host({key: value})

    assert key.calls == 0
    assert sanitized == {"<key:_StatefulKey>": "***"}
    assert SECRET_MARKER not in str(sanitized)


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
        node = _CountingMapping({f"field{level}_{index}ApiKey": SECRET_MARKER for index in range(97)})
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


@pytest.mark.parametrize(
    "container_value",
    [
        f"['{SECRET_MARKER}', 'second-list-secret']",
        (f"{{'primary': '{SECRET_MARKER}', 'nested': ['second-dict-secret', {{'ok': true}}]}}"),
        f"('{SECRET_MARKER}', ('second-tuple-secret', 2))",
    ],
)
def test_sanitizer_redacts_complete_unquoted_secret_container_assignment(
    container_value: str,
) -> None:
    """未加引号的嵌套容器凭据值必须整体遮蔽并保留后续字段。"""
    source = f"password={container_value}, operation=connect"

    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***, operation=connect"
    assert SECRET_MARKER not in sanitized
    assert "second-" not in sanitized


def test_sanitizer_fails_closed_for_unclosed_secret_container_assignment() -> None:
    """未闭合的凭据容器无法确认边界时遮蔽剩余文本。"""
    source = f"password=['{SECRET_MARKER}', 'unclosed-container-secret', operation=connect"

    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***"
    assert SECRET_MARKER not in sanitized
    assert "unclosed-container-secret" not in sanitized


def test_sanitizer_redacts_secret_tail_after_closed_assignment_container() -> None:
    """容器闭合符不代表凭据值结束，尾随内容也必须遮蔽。"""
    source = f"password=['{SECRET_MARKER}']tail-container-secret, operation=connect"

    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***, operation=connect"
    assert "tail-container-secret" not in sanitized


@pytest.mark.parametrize(
    "source",
    [
        f"password=[first\\], {SECRET_MARKER}], status=ok",
        f"password=(first\\), {SECRET_MARKER}), status=ok",
        "password={first\\}, " + SECRET_MARKER + "}, status=ok",
    ],
)
def test_sanitizer_ignores_escaped_assignment_container_closers(
    source: str,
) -> None:
    """未引号容器中的转义闭合符不得提前结束凭据扫描。"""
    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***, status=ok"
    assert SECRET_MARKER not in sanitized


@pytest.mark.parametrize(
    "source",
    [
        f"password=prefix[{SECRET_MARKER}, second-prefix-secret], status=ok",
        f"password=call({SECRET_MARKER}, second-call-secret), status=ok",
        (f"password=\\[{SECRET_MARKER}, second-escaped-open-secret], status=ok"),
    ],
)
def test_sanitizer_tracks_containers_after_unquoted_value_prefix(
    source: str,
) -> None:
    """未引号值任意位置的容器均须屏蔽其内部字段分隔符。"""
    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***, status=ok"
    assert SECRET_MARKER not in sanitized
    assert "second-" not in sanitized


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            f'password=prefix"{SECRET_MARKER}, second-double-secret", status=ok',
            "password=***, status=ok",
        ),
        (
            f"password=prefix'{SECRET_MARKER}, second-single-secret', status=ok",
            "password=***, status=ok",
        ),
        (
            f"message=\"password=prefix'{SECRET_MARKER}, second-inner-secret'\"; status=ok",
            'message="password=***"; status=ok',
        ),
    ],
)
def test_sanitizer_tracks_quoted_fragments_inside_unquoted_secret_value(
    source: str,
    expected: str,
) -> None:
    """值中途的 quoted fragment 不得让内部逗号提前结束脱敏。"""
    sanitized = str(sanitize_for_host(source))

    assert sanitized == expected
    assert SECRET_MARKER not in sanitized
    assert "second-" not in sanitized


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("escape_layers", [1, 2, 3])
def test_sanitizer_tracks_escaped_quoted_fragments_inside_secret_value(
    quote: str,
    escape_layers: int,
) -> None:
    """多层 slash-escaped quoted fragment 的内部逗号仍属于凭据值。"""
    wrapper = "\\" * escape_layers + quote
    source = f"password=prefix{wrapper}{SECRET_MARKER}, second-escaped-secret{wrapper}, status=ok"

    sanitized = str(sanitize_for_host(source))

    assert sanitized == "password=***, status=ok"
    assert SECRET_MARKER not in sanitized
    assert "second-escaped-secret" not in sanitized


def test_sanitizer_redacts_unquoted_multiword_secret_in_error_summary() -> None:
    """异常中的无引号多词凭据必须净化到可靠分隔符。"""
    summary = summarize_error(RuntimeError("password=alpha beta; operation=connect"))

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
        _DynamicSecretLocationInput(payload={SECRET_MARKER: "not-an-integer"})
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


def test_sanitizer_does_not_stringify_unsupported_leaf_objects() -> None:
    """未知叶对象只输出固定类型占位，不能执行无界字符串协议。"""

    class _UnsupportedLeaf:
        """记录 sanitizer 是否调用第三方字符串协议。"""

        calls = 0
        class_reads = 0

        def __getattribute__(self, name: str):
            if name == "__class__":
                type(self).class_reads += 1
            return object.__getattribute__(self, name)

        def __str__(self) -> str:
            type(self).calls += 1
            return SECRET_MARKER

    leaf = _UnsupportedLeaf()

    sanitized = sanitize_for_host(leaf)
    summary = summarize_result(leaf)

    assert _UnsupportedLeaf.calls == 0
    assert _UnsupportedLeaf.class_reads == 0
    assert sanitized == "<unavailable:_UnsupportedLeaf>"
    assert summary == "<unavailable:_UnsupportedLeaf>"
    assert SECRET_MARKER not in summary


def test_sanitizer_does_not_query_hostile_metaclass_for_dataclass_marker() -> None:
    """未知叶对象的 dataclass 分派不得执行自定义 metaclass 属性协议。"""

    class _HostileMeta(type):
        dataclass_reads = 0

        def __getattribute__(cls, name: str):
            if name == "__dataclass_fields__":
                reads = type.__getattribute__(_HostileMeta, "dataclass_reads")
                type.__setattr__(_HostileMeta, "dataclass_reads", reads + 1)
                raise RuntimeError(SECRET_MARKER)
            return type.__getattribute__(cls, name)

    class _UnsupportedLeaf(metaclass=_HostileMeta):
        pass

    sanitized = sanitize_for_host(_UnsupportedLeaf())

    assert type.__getattribute__(_HostileMeta, "dataclass_reads") == 0
    assert sanitized == "<unavailable:_UnsupportedLeaf>"
    assert SECRET_MARKER not in sanitized


def test_sanitizer_reads_exception_args_without_custom_string_protocol() -> None:
    """异常摘要保留安全参数，但不得调用异常子类的自定义字符串协议。"""

    class _HostileError(RuntimeError):
        """通过字符串协议回显凭据的第三方异常。"""

        calls = 0
        class_reads = 0

        def __getattribute__(self, name: str):
            if name in ("__class__", "args"):
                type(self).class_reads += 1
            return RuntimeError.__getattribute__(self, name)

        def __str__(self) -> str:
            type(self).calls += 1
            return SECRET_MARKER

    error = _HostileError("operation=connect")

    summary = summarize_error(error)

    assert _HostileError.calls == 0
    assert _HostileError.class_reads == 0
    assert "operation=connect" in summary
    assert SECRET_MARKER not in summary


def test_sanitizer_bounds_json_shaped_text_before_parsing() -> None:
    """超过文本上限的 JSON 外形输入不得触发完整解析。"""
    secret_marker = "oversized-json-secret-9056"
    source = '{"password":"' + secret_marker + '","padding":"' + "x" * 20000 + '"}'

    with patch(
        "app.agent.policy.sanitizer.json.loads",
        side_effect=AssertionError("oversized JSON must not be parsed"),
    ) as mock_loads:
        sanitized = str(sanitize_for_host(source))

    mock_loads.assert_not_called()
    assert secret_marker not in sanitized
    assert sanitized.endswith("<truncated>")
    assert len(sanitized) < 17000


def test_sanitizer_fails_closed_for_oversized_json_identity_values() -> None:
    """超长 JSON 无法确认对象身份时，窗口内通用值字段必须脱敏。"""
    secret_marker = "oversized-setting-secret-marker"
    source = json.dumps(
        {
            "setting_key": "API_TOKEN",
            "value_preview": secret_marker,
            "value": secret_marker + "x" * sanitizer_module._MAX_TEXT_CHARS,
        }
    )

    with patch(
        "app.agent.policy.sanitizer.json.loads",
        side_effect=AssertionError("oversized JSON must not be parsed"),
    ) as mock_loads:
        sanitized = str(sanitize_for_host(source))

    mock_loads.assert_not_called()
    assert secret_marker not in sanitized
    assert '"value_preview": ***' in sanitized
    assert '"value": ***' in sanitized
    assert sanitized.endswith("<truncated>")


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
