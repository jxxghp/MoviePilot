"""MoviePilot 结构化 API 网关工具。"""

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Type, Union, cast

from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from app.agent.api.arguments import (
    api_input_contract,
    canonical_api_arguments,
    coerce_string_body,
)
from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import ExecutionOutcome, PrincipalRole
from app.agent.policy.sanitizer import summarize_input
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.result import inspect_tool_result
from app.agent.tools.tags import ToolTag
from app.schemas.common import JsonData
from app.schemas.types import NotificationChannel

_TOOL_MESSAGE_OPERATION_MAX_CHARS = 96
_TOOL_MESSAGE_PARAMETER_MAX_CHARS = 320
_SEARCH_TORRENTS_TOOL_TIMEOUT_SECONDS = 300.0

# API 网关顶层请求体只开放对象、数组和 system.upgrade.dev 的固定字符串。
MoviePilotApiBody = Union[Dict[str, JsonData], list[JsonData], Literal["dev"], None]


@lru_cache(maxsize=1)
def _load_api_mcp_input_schema() -> dict[str, Any]:
    """读取由业务 OpenAPI 生成并经漂移测试锁定的外部 MCP schema。"""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "policy"
        / "resources"
        / "api_mcp_schema.json"
    )
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("oneOf"), list):
        raise RuntimeError("moviepilot_api MCP schema 无效")
    return payload


class MoviePilotApiInput(BaseModel):  # type: ignore[misc]
    """MoviePilot API 网关的结构化输入参数。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(
        ...,
        description=(
            "Exact allowlisted MoviePilot operation ID selected from the loaded domain Skill. "
            "Never supply a URL, authentication header, or API token."
        ),
    )
    path_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Route placeholders only, e.g. media.detail uses {\"media_id\": \"27205\"}.",
    )
    query: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Query fields declared by this operation, never flattened at the top level. "
            "Pagination is operation-specific: subscription.execution.list uses limit, not page/count."
        ),
    )
    body: MoviePilotApiBody = Field(
        default=None,
        description=(
            "Request body declared by the selected operation and its loaded Skill contract. "
            "Pass objects and arrays, including nested file items, as native JSON values; "
            "pass null only when the selected operation allows it. "
            "The only string body is the exact value 'dev' for system.upgrade.dev."
        ),
    )

    @field_validator("body", mode="before")
    @classmethod
    def _restore_json_string_body(cls, value: Any) -> Any:
        """模型在联合类型 schema 下可能把 body 编码为 JSON 字符串，入模前还原。"""
        return coerce_string_body(value)


class MoviePilotApiTool(MoviePilotTool):
    """
    MoviePilot 结构化 API 网关。

    网关只接受固定 operation ID 和结构化参数，由宿主注册表解析为固定 API
    方法与路径；模型不能提供 URL、认证头、令牌或任意 HTTP 方法。
    """

    name: str = "moviepilot_api"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
        ToolTag.Subscription,
        ToolTag.Download,
        ToolTag.Site,
        ToolTag.Plugin,
    ]
    description: str = (
        "Call allowlisted MoviePilot business APIs through operation-specific input contracts. "
        "Load the relevant domain Skill before calling and use operation error feedback to correct inputs. "
        "The ONLY top-level keys are operation_id, path_params, query, body. "
        "Examples (replace sample IDs with IDs from prior results): "
        '{"operation_id":"media.detail","path_params":{"media_id":"27205"},'
        '"query":{"media_source":"tmdb","type_name":"电影"}}; '
        '{"operation_id":"subscription.execution.list","query":{"limit":10}}; '
        '{"operation_id":"site.rss","query":{"page":1,"count":20}}. '
        "Never invent filters or copy pagination fields from another operation. "
        "Arbitrary URLs, commands, authentication endpoints, headers, and tokens are forbidden."
    )
    require_admin: bool = False
    args_schema: Type[BaseModel] = MoviePilotApiInput
    handle_tool_error: bool = True

    _executor: Optional[MoviePilotApiExecutor] = PrivateAttr(default=None)

    def __init__(
        self,
        session_id: str,
        user_id: str,
        *,
        executor: Optional[MoviePilotApiExecutor] = None,
        **kwargs: Any,
    ) -> None:
        """注入可测试的固定 API 执行器。"""
        super().__init__(session_id=session_id, user_id=user_id, **kwargs)
        self._executor = executor

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """生成包含脱敏、有界主要参数的结构化 API 调用提示。"""
        operation_id = summarize_input(
            kwargs.get("operation_id") or "未知操作",
            max_chars=_TOOL_MESSAGE_OPERATION_MAX_CHARS,
        )
        message = f"调用 MoviePilot API：{operation_id}"
        parameters = {
            field_name: field_value
            for field_name in ("path_params", "query", "body")
            if (field_value := kwargs.get(field_name)) is not None
            and not (isinstance(field_value, dict) and not field_value)
        }
        if not parameters:
            return message
        parameter_summary = summarize_input(
            parameters,
            max_chars=_TOOL_MESSAGE_PARAMETER_MAX_CHARS,
        )
        return f"{message}，主要参数：{parameter_summary}"

    def _get_run_timeout_seconds(self, **kwargs: Any) -> Optional[float]:
        """将 search.torrents 的总工具等待时间限制为五分钟。"""
        if kwargs.get("operation_id") == "search.torrents":
            return _SEARCH_TORRENTS_TOOL_TIMEOUT_SECONDS
        return super()._get_run_timeout_seconds(**kwargs)

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """返回包含全部白名单 operation 精确参数的 MCP JSON Schema。"""
        return deepcopy(_load_api_mcp_input_schema())

    @property
    def tool_call_schema(self) -> Type[BaseModel]:
        """本工具没有注入参数，直接保留原模型，防止子集投影丢失 extra=forbid。"""
        return MoviePilotApiInput

    def _parse_input(
        self, tool_input: Union[str, dict[str, Any]], tool_call_id: Optional[str],
    ) -> Union[str, dict[str, Any]]:
        """在 LangChain 解析前校验原始参数，让只读调用也能收到安全纠错回执。"""
        if isinstance(tool_input, dict):
            operation_id = str(tool_input.get("operation_id") or "")
            try:
                self.canonical_arguments(tool_input)
            except (TypeError, ValueError) as error:
                raise ToolException(self._invalid_input_result(operation_id, error)) from error
        return cast(Union[str, dict[str, Any]], super()._parse_input(tool_input, tool_call_id))

    def canonical_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """用缓存的 operation 合同生成实际执行与持久指纹共用的参数。"""
        return canonical_api_arguments(arguments, _load_api_mcp_input_schema())

    def get_operation_input_contract(self, operation_id: str) -> dict[str, Any]:
        """返回单个 operation 的有界参数合同，供失败回执指导模型重试。"""
        return api_input_contract(operation_id, _load_api_mcp_input_schema())

    def _invalid_input_result(self, operation_id: str, error: Exception) -> str:
        """把参数合同错误转换为不包含原始请求值的模型纠错回执。"""
        contract = self.get_operation_input_contract(operation_id)
        return json.dumps(
            {
                "success": False,
                "error": "invalid_input",
                "operation_id": operation_id,
                "message": (
                    f"{operation_id} 的输入不符合已声明合同（{str(error)}）。"
                    "请按 input_contract 只提交允许字段并补齐 required 字段后重试。"
                ),
                "input_contract": contract,
            },
            ensure_ascii=False,
        )

    async def _resolve_superuser_integration_identity(
        self,
    ) -> tuple[str, Optional[str], bool]:
        """为已验证的管理员集成解析一个真实持久化超级管理员身份。"""
        from app.application.security.auth import build_superuser_token_payload

        payload = await self.run_blocking(
            "db",
            build_superuser_token_payload,
        )
        if payload.sub is None:
            raise ApiExecutionError("管理员集成身份没有持久化用户 ID")
        return str(payload.sub), payload.username, bool(payload.super_user)

    async def _resolve_api_identity(
        self,
        *,
        require_system_admin: bool = False,
    ) -> tuple[str, Optional[str], bool]:
        """把 Web、渠道或集成身份解析为真实 MoviePilot API 用户身份。"""
        raw_user_id = str(self._user_id or "")
        if self._source == "api" and bool(self._agent_context.get("is_admin")):
            return await self._resolve_superuser_integration_identity()
        direct_user_channels = {
            NotificationChannel.Web.value,
            NotificationChannel.WebAgent.value,
        }
        is_direct_user_channel = self._channel in direct_user_channels
        if (
            require_system_admin
            and bool(self._agent_context.get("is_admin"))
            and not is_direct_user_channel
        ):
            # 通知渠道管理员延续旧管理员工具语义，但只在管理员 operation
            # 上借用系统管理员集成身份；普通 operation 仍解析其绑定用户。
            return await self._resolve_superuser_integration_identity()
        if self._data is None:
            if is_direct_user_channel and raw_user_id.isdigit():
                return raw_user_id, self._username, bool(self._agent_context.get("is_admin"))
            raise ApiExecutionError("Agent 数据上下文未装配，无法解析 API 用户身份")

        username = self._username
        user = await self._data.users.async_get_by_name(username) if username else None
        if user is None and not is_direct_user_channel and self._channel and raw_user_id:
            try:
                channel = NotificationChannel(self._channel)
            except ValueError:
                channel = None
            binding_keys = (
                {
                    NotificationChannel.Telegram: ("telegram_userid",),
                    NotificationChannel.Discord: ("discord_userid",),
                    NotificationChannel.Wechat: ("wechat_userid",),
                    NotificationChannel.Feishu: ("feishu_userid", "feishu_openid"),
                    NotificationChannel.WechatClawBot: ("wechatclawbot_userid",),
                    NotificationChannel.Slack: ("slack_userid",),
                    NotificationChannel.VoceChat: ("vocechat_userid",),
                    NotificationChannel.SynologyChat: ("synologychat_userid",),
                    NotificationChannel.QQ: ("qq_userid", "qq_openid"),
                }.get(channel)
                if channel is not None
                else None
            )
            if binding_keys:
                username = await self.run_blocking(
                    "db",
                    self._data.users.find_name_by_bindings,
                    {key: raw_user_id for key in binding_keys},
                )
                user = await self._data.users.async_get_by_name(username) if username else None
        if user is None and is_direct_user_channel and raw_user_id.isdigit():
            return raw_user_id, self._username, bool(self._agent_context.get("is_admin"))
        if user is None or not user.is_active:
            raise ApiExecutionError("当前 Agent 身份未绑定有效的 MoviePilot 用户")
        return str(user.id), user.name, bool(user.is_superuser)

    async def _get_executor(
        self,
        *,
        require_system_admin: bool = False,
    ) -> tuple[MoviePilotApiExecutor, bool]:
        """返回当前 operation 的执行器及其管理员身份事实。"""
        if self._executor is not None:
            return self._executor, await self.is_admin_user()
        user_id, username, is_admin = await self._resolve_api_identity(
            require_system_admin=require_system_admin,
        )
        return (
            MoviePilotApiExecutor(
                context=ApiExecutionContext(
                    user_id=user_id,
                    username=username,
                    is_admin=is_admin,
                    session_id=self._session_id,
                    channel=self._channel,
                    source=self._source,
                )
            ),
            is_admin,
        )

    async def run(  # type: ignore[override]
        self,
        operation_id: str,
        path_params: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        body: Any = None,
        **kwargs: Any,
    ) -> str:
        """
        执行一个白名单 MoviePilot API 操作。

        :param operation_id: 稳定 API 操作标识
        :param path_params: 路径参数
        :param query: 查询参数
        :param body: JSON 请求体
        :return: 结构化业务结果或安全错误消息
        """
        spec = resolve_api_operation(operation_id)
        if spec is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "unknown_operation",
                    "message": f"未允许的 MoviePilot API 操作：{operation_id}",
                },
                ensure_ascii=False,
            )
        try:
            arguments: dict[str, Any] = {"operation_id": operation_id, **kwargs}
            if path_params is not None:
                arguments["path_params"] = path_params
            if query is not None:
                arguments["query"] = query
            if body is not None:
                arguments["body"] = body
            self.canonical_arguments(arguments)
        except (TypeError, ValueError) as error:
            return self._invalid_input_result(operation_id, error)
        try:
            requires_system_admin = spec.required_role is PrincipalRole.SYSTEM_ADMIN
            executor, is_admin = await self._get_executor(
                require_system_admin=requires_system_admin,
            )
            if requires_system_admin and not is_admin:
                return json.dumps(
                    {
                        "success": False,
                        "error": "permission_denied",
                        "message": "This MoviePilot API operation requires a system administrator.",
                    },
                    ensure_ascii=False,
                )
            result = await executor.execute(
                operation_id,
                path_params=path_params,
                query=query,
                body=body,
            )
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("error") == "api_error"
                and payload.get("status_code") in {400, 422}
            ):
                payload["operation_id"] = operation_id
                payload["message"] = (
                    f"{operation_id} 被 API 拒绝，请根据 input_contract 修正输入后重试。"
                )
                payload["input_contract"] = self.get_operation_input_contract(operation_id)
                result = json.dumps(payload, ensure_ascii=False)
            if operation_id == "scheduler.run" and inspect_tool_result(result) is ExecutionOutcome.SUCCEEDED:
                payload = json.loads(result)
                if isinstance(payload, dict) and payload.get("success") is True:
                    payload["execution_outcome"] = "pending"
                    return json.dumps(payload, ensure_ascii=False)
            return result
        except ApiExecutionError as error:
            failure: dict[str, Any] = {
                "success": False,
                "error": "operation_unavailable",
                "message": str(error),
            }
            if error.external_may_continue:
                failure["execution_outcome"] = "unknown"
                failure["message"] += "；操作可能已生效，请先只读核验实际状态，避免重复执行。"
            return json.dumps(
                failure,
                ensure_ascii=False,
            )


__all__ = ["MoviePilotApiInput", "MoviePilotApiTool"]
