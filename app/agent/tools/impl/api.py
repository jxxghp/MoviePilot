"""
MoviePilot 结构化 API 网关工具。
"""

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Type, Union

from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from app.agent.api.arguments import (
    api_input_contract,
    canonical_api_arguments,
    coerce_string_body,
)
from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.policy.api import API_UNBOUND_READ_OPERATIONS, resolve_api_operation
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
    """
    读取由业务 OpenAPI 生成并经漂移测试锁定的外部 MCP schema。
    """
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


class MoviePilotApiInput(BaseModel):
    """
    MoviePilot API 网关的结构化输入参数。
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(
        ...,
        description="从已加载领域技能中选择的精确白名单操作标识；不得提供 URL、认证请求头或 API 令牌",
    )
    path_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="仅填写路由占位参数，例如 media.detail 使用 {\"media_id\": \"27205\"}",
    )
    query: Dict[str, Any] = Field(
        default_factory=dict,
        description="该操作声明的查询字段，不得平铺到顶层；分页规则因操作而异，例如 subscription.execution.list 使用 limit",
    )
    body: MoviePilotApiBody = Field(
        default=None,
        description=(
            "按所选操作及已加载技能契约提供请求体。"
            "对象、数组及嵌套文件项必须使用原生 JSON 值；"
            "仅在操作允许时传 null。"
            "唯一的字符串请求体是 system.upgrade.dev 要求的精确值 dev"
        ),
    )

    # Pydantic 校验器由运行时注册，保留第三方装饰器所需的动态类型兼容。
    @field_validator("body", mode="before")
    @classmethod
    def _restore_json_string_body(cls, value: Any) -> Any:
        """
        模型在联合类型 schema 下可能把 body 编码为 JSON 字符串，入模前还原。
        """
        return coerce_string_body(value)


class MoviePilotApiTool(MoviePilotTool):
    """
    MoviePilot 结构化 API 网关。

    网关只接受固定 operation ID 和结构化参数，由宿主注册表解析为固定 API
    方法与路径；模型不能提供 URL、认证头、令牌或任意 HTTP 方法。

    :ivar _executor: 延迟构造的白名单业务 API 执行器；首次调用前为 None
    """

    name: str = Field(
        default="moviepilot_api",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[
            ToolTag.Read,
            ToolTag.Media,
            ToolTag.Subscription,
            ToolTag.Download,
            ToolTag.Site,
            ToolTag.Plugin,
        ],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "按各操作的输入契约调用白名单 MoviePilot 业务 API。"
            "调用前先加载对应领域 Skill，并根据操作错误反馈修正输入。"
            "顶层仅允许 operation_id、path_params、query、body。"
            "示例中的标识须替换为此前结果中的实际标识：{\"operation_id\":\"media.detail\",\"path_params\":{\"media_id\":\"27205\"}"
            ",\"query\":{\"media_source\":\"tmdb\",\"type_name\":\"电影\"}}；"
            "{\"operation_id\":\"subscription.execution.list\",\"query\":{\"limit\":10}}；"
            "{\"operation_id\":\"site.rss\",\"query\":{\"page\":1,\"count\":20}}。"
            "不得编造筛选条件或套用其他操作的分页字段。"
            "禁止任意 URL、命令、认证端点、请求头和令牌。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    require_admin: bool = Field(
        default=False,
        description="是否仅允许系统管理员调用",
    )
    args_schema: Type[BaseModel] = Field(
        default=MoviePilotApiInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    handle_tool_error: bool = Field(
        default=True,
        description="是否将工具异常转换为可供模型修正输入的错误结果",
    )

    _executor: Optional[MoviePilotApiExecutor] = PrivateAttr(default=None)

    def __init__(
        self,
        session_id: str,
        user_id: str,
        *,
        executor: Optional[MoviePilotApiExecutor] = None,
        **kwargs: Any,
    ) -> None:
        """
        注入可测试的固定 API 执行器。
        """
        super().__init__(session_id=session_id, user_id=user_id, **kwargs)
        self._executor = executor

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        生成包含脱敏、有界主要参数的结构化 API 调用提示。
        """
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
        """
        将 search.torrents 的总工具等待时间限制为五分钟。
        """
        if kwargs.get("operation_id") == "search.torrents":
            return _SEARCH_TORRENTS_TOOL_TIMEOUT_SECONDS
        return super()._get_run_timeout_seconds(**kwargs)

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """
        返回包含全部白名单 operation 精确参数的 MCP JSON Schema。
        """
        return deepcopy(_load_api_mcp_input_schema())

    @property
    def tool_call_schema(self) -> Type[BaseModel]:
        """
        本工具没有注入参数，直接保留原模型，防止子集投影丢失 extra=forbid。
        """
        return MoviePilotApiInput

    def _parse_input(
        self, tool_input: Union[str, dict[str, Any]], tool_call_id: Optional[str] = None,
    ) -> Union[str, dict[str, Any]]:
        """
        在 LangChain 解析前校验原始参数，让只读调用也能收到安全纠错回执。
        """
        if isinstance(tool_input, dict):
            operation_id = str(tool_input.get("operation_id") or "")
            try:
                self.canonical_arguments(tool_input)
            except (TypeError, ValueError) as error:
                raise ToolException(self._invalid_input_result(operation_id, error)) from error
        return super()._parse_input(tool_input, tool_call_id)

    def canonical_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        用缓存的 operation 合同生成实际执行与持久指纹共用的参数。
        """
        return canonical_api_arguments(arguments, _load_api_mcp_input_schema())

    def get_operation_input_contract(self, operation_id: str) -> dict[str, Any]:
        """
        返回单个 operation 的有界参数合同，供失败回执指导模型重试。
        """
        return api_input_contract(operation_id, _load_api_mcp_input_schema())

    def _invalid_input_result(self, operation_id: str, error: Exception) -> str:
        """
        把参数合同错误转换为不包含原始请求值的模型纠错回执。
        """
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
        """
        为已验证的管理员集成解析一个真实持久化超级管理员身份。
        """
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
        """
        把 Web、渠道或集成身份解析为真实 MoviePilot API 用户身份。
        """
        raw_user_id = str(self._user_id or "")
        is_authenticated_integration = self._source == "api" or (
            self._channel == NotificationChannel.Web.value
            and self._source in {"openai", "openai.responses", "anthropic"}
        )
        if is_authenticated_integration and bool(self._agent_context.get("is_admin")):
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

        if is_direct_user_channel:
            # Web 入口的持久化用户 ID 是事实源，旧会话用户名不能切换到同名新账号。
            user = await self._data.users.async_get_by_id(int(raw_user_id)) if raw_user_id.isdigit() else None
        elif not self._channel and not self._source:
            # 无渠道的后台任务使用宿主持久化的用户名，保留定时任务的执行身份。
            user = await self._data.users.async_get_by_name(self._username) if self._username else None
        else:
            user = await self._resolve_channel_api_user(raw_user_id)
        if user is None:
            raise ApiExecutionError(self._identity_unavailable_message())
        if not user.is_active:
            raise ApiExecutionError("当前 MoviePilot 用户已停用，请联系管理员启用该用户后重试。重新绑定不能解除停用。")
        return str(user.id), user.name, bool(user.is_superuser)

    def _identity_unavailable_message(self) -> str:
        """
        按真实前端入口提示绑定步骤；查询仅能确定没有唯一启用绑定，不臆断具体原因。
        """
        labels = {
            NotificationChannel.Telegram.value: "Telegram用户",
            NotificationChannel.Discord.value: "Discord用户",
            NotificationChannel.Wechat.value: "企业微信用户",
            NotificationChannel.Feishu.value: "飞书用户",
            NotificationChannel.WechatClawBot.value: "微信 ClawBot 用户",
            NotificationChannel.Slack.value: "Slack用户",
            NotificationChannel.VoceChat.value: "VoceChat用户",
            NotificationChannel.SynologyChat.value: "SynologyChat用户",
            NotificationChannel.QQ.value: "QQ 用户 ID 或 QQ 用户 OpenID（填写与当前发送者 ID 对应的一项）",
        }
        label = labels.get(self._channel or "")
        if label is None:
            if self._channel in {NotificationChannel.Web.value, NotificationChannel.WebAgent.value}:
                return "当前登录身份未对应有效的 MoviePilot 用户，请重新登录；若账号已删除，请联系管理员。此入口无需绑定通知渠道。"
            return "当前任务身份未对应有效的 MoviePilot 用户，请联系管理员检查任务所属账号及身份配置。"
        sender_id = json.dumps(str(self._user_id or ""), ensure_ascii=False)
        return (
            "当前渠道账号未绑定唯一且已启用的 MoviePilot 用户，可能尚未绑定、绑定用户已停用或存在重复绑定。"
            f"请登录要使用的 MoviePilot 账号，打开右上角头像 → 个人信息 → 账号绑定，在「{label}」中填写"
            f"当前渠道发送者 ID {sender_id}，然后保存并重试。请填写完整 ID，不要填写昵称、群 ID 或机器人 ID。"
            "若已经填写，请管理员确认该用户已启用，且同一渠道 ID 没有绑定到多个启用用户。"
        )

    async def _resolve_channel_api_user(self, raw_user_id: str) -> Any:
        """
        只按渠道稳定 ID 查找绑定用户，昵称和展示用户名不构成认证依据。
        """
        data = self._data
        if data is None:
            return None
        try:
            channel = NotificationChannel(self._channel)
        except (TypeError, ValueError):
            return None
        binding_keys = {
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
        if not binding_keys or not raw_user_id:
            return None
        # userid/openid 是候选字段，仓储必须拒绝匹配到多个用户的歧义绑定。
        username = await self.run_blocking(
            "db", data.users.find_name_by_bindings,
            {key: raw_user_id for key in binding_keys}, match_any=True,
        )
        return await data.users.async_get_by_name(username) if username else None

    async def _get_executor(
        self,
        *,
        require_system_admin: bool = False,
        public_read_only: bool = False,
    ) -> tuple[MoviePilotApiExecutor, bool]:
        """
        解析执行身份；公共资料使用只能执行白名单读取的宿主认证通道。
        """
        if self._executor is not None:
            return self._executor, await self.is_admin_user()
        if public_read_only:
            user_id, username, is_admin = await self._resolve_superuser_integration_identity()
        else:
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
                    public_read_only=public_read_only,
                )
            ),
            is_admin,
        )

    # 具体参数由 args_schema 验证，执行入口保留与工具基类的动态分发兼容。
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

        HTTP/MCP/CLI 的直接调用不经过输入模型，因此也要还原顶层 JSON
        字符串请求体，并把还原后的值用于校验和实际请求。

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
            body = coerce_string_body(body)
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
                public_read_only=operation_id in API_UNBOUND_READ_OPERATIONS,
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
