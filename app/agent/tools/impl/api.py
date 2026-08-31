"""MoviePilot 结构化 API 网关工具。"""

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field, PrivateAttr

from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.policy.api import resolve_api_operation
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
            "稳定的 MoviePilot API operation ID。先根据领域 Skill 选择操作，不要传入 URL、认证头或 API Token。"
        ),
    )
    path_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="路径参数；仅填当前 operation 声明的参数。",
    )
    query: Dict[str, Any] = Field(
        default_factory=dict,
        description="查询参数；仅填当前 operation 声明的参数。",
    )
    body: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON 请求体；字段由当前 operation 的 Skill 合同定义。",
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
        "调用经过白名单审核的 MoviePilot 业务 API。使用领域 Skill 获取 operation_id、"
        "参数和失败处理；外部 MCP tools/list 会为每个 operation 提供完整 oneOf 参数合同；"
        "不能调用任意 URL、命令或认证接口。"
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

    async def _resolve_api_identity(self) -> tuple[str, Optional[str], bool]:
        """把 Web 或渠道身份解析为真实 MoviePilot 用户身份。"""
        raw_user_id = str(self._user_id or "")
        if self._source == "api" and bool(self._agent_context.get("is_admin")):
            from app.application.security.auth import build_superuser_token_payload

            payload = await self.run_blocking(
                "db",
                build_superuser_token_payload,
            )
            if payload.sub is None:
                raise ApiExecutionError("管理员集成身份没有持久化用户 ID")
            return str(payload.sub), payload.username, bool(payload.super_user)
        direct_user_channels = {
            NotificationChannel.Web.value,
            NotificationChannel.WebAgent.value,
        }
        is_direct_user_channel = self._channel in direct_user_channels
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

    async def _get_executor(self) -> MoviePilotApiExecutor:
        """返回注入执行器，未注入时按当前可信 Agent 身份创建。"""
        if self._executor is None:
            user_id, username, is_admin = await self._resolve_api_identity()
            self._executor = MoviePilotApiExecutor(
                context=ApiExecutionContext(
                    user_id=user_id,
                    username=username,
                    is_admin=is_admin,
                    session_id=self._session_id,
                    channel=self._channel,
                    source=self._source,
                )
            )
        return self._executor

    async def run(  # type: ignore[override]
        self,
        operation_id: str,
        path_params: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
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
            executor = await self._get_executor()
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
