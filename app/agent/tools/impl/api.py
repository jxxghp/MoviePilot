"""MoviePilot 结构化 API 网关工具。"""

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field, PrivateAttr

from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import PrincipalRole
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.schemas.types import NotificationChannel


@lru_cache(maxsize=1)
def _load_api_mcp_input_schema() -> dict[str, Any]:
    """读取由业务 OpenAPI 生成并经漂移测试锁定的外部 MCP schema。"""
    schema_path = Path(__file__).resolve().parents[2] / "policy" / "api_mcp_schema.json"
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("oneOf"), list):
        raise RuntimeError("moviepilot_api MCP schema 无效")
    return payload


class MoviePilotApiInput(BaseModel):  # type: ignore[misc]
    """MoviePilot API 网关的结构化输入参数。"""

    operation_id: str = Field(
        ...,
        description=(
            "Exact allowlisted MoviePilot operation ID selected from the loaded domain Skill. "
            "Never supply a URL, authentication header, or API token."
        ),
    )
    path_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Route placeholder values declared by the selected operation.",
    )
    query: Dict[str, Any] = Field(
        default_factory=dict,
        description="Query-string fields declared by the selected operation.",
    )
    body: Any = Field(
        default=None,
        description=(
            "JSON request value declared by the selected operation and its loaded Skill contract. "
            "Most operations use an object; a oneOf branch may require an exact scalar."
        ),
    )


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
        "Call allowlisted MoviePilot business APIs. Use the domain Skill to select operation_id, "
        "parameters, and failure handling. For collection counts, use the smallest documented "
        "page and read collection.total_count instead of querying the database after item "
        "truncation. External MCP tools/list exposes one complete oneOf branch per operation. "
        "Arbitrary URLs, commands, and authentication endpoints are forbidden."
    )
    require_admin: bool = False
    args_schema: Type[BaseModel] = MoviePilotApiInput

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
        """生成结构化 API 调用提示。"""
        operation_id = kwargs.get("operation_id") or "未知操作"
        return f"调用 MoviePilot API：{operation_id}"

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """返回包含全部白名单 operation 精确参数的 MCP JSON Schema。"""
        return deepcopy(_load_api_mcp_input_schema())

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
        del kwargs
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
            return await executor.execute(
                operation_id,
                path_params=path_params,
                query=query,
                body=body,
            )
        except ApiExecutionError as error:
            return json.dumps(
                {
                    "success": False,
                    "error": "operation_unavailable",
                    "message": str(error),
                },
                ensure_ascii=False,
            )


__all__ = ["MoviePilotApiInput", "MoviePilotApiTool"]
