import json
from abc import ABCMeta, abstractmethod
from typing import Any, Optional

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from app.agent import StreamingHandler
from app.chain import ChainBase
from app.core.config import settings
from app.db.user_oper import UserOper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import Notification
from app.schemas.types import MessageChannel


class ToolChain(ChainBase):
    pass


class MoviePilotTool(BaseTool, metaclass=ABCMeta):
    """
    MoviePilot专用工具基类（LangChain v1 / langchain_core）
    """

    _session_id: str = PrivateAttr()
    _user_id: str = PrivateAttr()
    _channel: Optional[str] = PrivateAttr(default=None)
    _source: Optional[str] = PrivateAttr(default=None)
    _username: Optional[str] = PrivateAttr(default=None)
    _stream_handler: Optional[StreamingHandler] = PrivateAttr(default=None)
    _require_admin: bool = PrivateAttr(default=False)
    _agent_context: dict = PrivateAttr(default_factory=dict)

    def __init__(self, session_id: str, user_id: str, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._user_id = user_id
        self._require_admin = getattr(self.__class__, "require_admin", False)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("MoviePilotTool 只支持异步调用，请使用 _arun")

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """
        异步运行工具，负责：
        1. 在工具调用前将流式消息推送给用户
        2. 持久化工具调用记录到会话记忆
        3. 调用具体工具逻辑（子类实现的 execute 方法）
        4. 持久化工具结果到会话记忆
        5. 权限检查
        """

        permission_result = await self._check_permission()
        if permission_result:
            return permission_result

        # 获取工具执行提示消息
        tool_message = self.get_tool_message(**kwargs)
        if not tool_message:
            explanation = kwargs.get("explanation")
            if explanation:
                tool_message = explanation

        # 发送工具执行过程消息
        if self._stream_handler and self._stream_handler.is_streaming:
            if settings.AI_AGENT_VERBOSE:
                if self._stream_handler.is_auto_flushing:
                    # 渠道支持编辑：工具消息追加到 buffer，由定时刷新推送
                    if tool_message:
                        self._stream_handler.emit(f"\n\n⚙️ => {tool_message}\n\n")
                else:
                    # 渠道不支持编辑：取出 Agent 文字 + 工具消息合并独立发送
                    agent_message = await self._stream_handler.take()
                    messages = []
                    if agent_message:
                        messages.append(agent_message)
                    if tool_message:
                        messages.append(f"⚙️ => {tool_message}")
                    if messages:
                        merged_message = "\n\n".join(messages)
                        await self.send_tool_message(merged_message)
            else:
                # 非VERBOSE，重置缓冲区从头更新，保持消息编辑能力
                self._stream_handler.reset()
        else:
            # 未启用流式传输，不发送任何工具消息内容
            pass

        logger.debug(f"Executing tool {self.name} with args: {kwargs}")

        # 执行具体工具逻辑
        try:
            result = await self.run(**kwargs)
            logger.debug(f"Tool {self.name} executed with result: {result}")
        except Exception as e:
            error_message = f"工具执行异常 ({type(e).__name__}): {str(e)}"
            logger.error(f"Tool {self.name} execution failed: {e}", exc_info=True)
            result = error_message

        # 格式化结果
        if isinstance(result, str):
            formatted_result = result
        elif isinstance(result, (int, float)):
            formatted_result = str(result)
        else:
            formatted_result = json.dumps(result, ensure_ascii=False, indent=2)

        return formatted_result

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """
        获取工具执行时的友好提示消息。

        子类可以重写此方法，根据实际参数生成个性化的提示消息。
        如果返回 None 或空字符串，将回退使用 explanation 参数。

        Args:
            **kwargs: 工具的所有参数（包括 explanation）

        Returns:
            str: 友好的提示消息，如果返回 None 或空字符串则使用 explanation
        """
        return None

    @abstractmethod
    async def run(self, **kwargs) -> str:
        """子类实现具体的工具执行逻辑"""
        raise NotImplementedError

    def set_message_attr(self, channel: str, source: str, username: str):
        """
        设置消息属性
        """
        self._channel = channel
        self._source = source
        self._username = username

    def set_stream_handler(self, stream_handler: StreamingHandler):
        """
        设置回调处理器
        """
        self._stream_handler = stream_handler

    def set_agent_context(self, agent_context: Optional[dict]):
        """
        设置与当前 Agent 共享的上下文。
        """
        self._agent_context = agent_context or {}

    async def _check_permission(self) -> Optional[str]:
        """
        检查用户权限：
        1. 首先检查工具是否需要管理员权限
        2. 如果需要管理员权限，则检查用户是否是渠道管理员
        3. 如果渠道没有设置管理员名单，则检查用户是否是系统管理员
        4. 如果都不是系统管理员，检查用户ID是否等于渠道配置的用户ID
        5. 如果都不是，返回权限拒绝消息
        """
        if not self._require_admin:
            return None

        if not self._channel or not self._source:
            return None

        user_id_str = str(self._user_id) if self._user_id else None

        channel_type_map = {
            MessageChannel.Telegram: "telegram",
            MessageChannel.Discord: "discord",
            MessageChannel.Wechat: "wechat",
            MessageChannel.Slack: "slack",
            MessageChannel.VoceChat: "vocechat",
            MessageChannel.SynologyChat: "synologychat",
            MessageChannel.QQ: "qqbot",
        }

        channel_type = None
        for key, value in channel_type_map.items():
            if self._channel == key.value:
                channel_type = value
                break

        if not channel_type:
            return None

        admin_key_map = {
            "telegram": "TELEGRAM_ADMINS",
            "discord": "DISCORD_ADMINS",
            "wechat": "WECHAT_ADMINS",
            "slack": "SLACK_ADMINS",
            "vocechat": "VOCECHAT_ADMINS",
            "synologychat": "SYNOLOGYCHAT_ADMINS",
            "qqbot": "QQBOT_ADMINS",
        }

        user_id_key_map = {
            "telegram": "TELEGRAM_CHAT_ID",
            "vocechat": "VOCECHAT_CHANNEL_ID",
            "wechat": "WECHAT_BOT_CHAT_ID",
        }

        admin_key = admin_key_map.get(channel_type)
        user_id_key = user_id_key_map.get(channel_type)

        try:
            configs = ServiceConfigHelper.get_notification_configs()
            for config in configs:
                if config.name == self._source and config.config:
                    channel_admins = config.config.get(admin_key) if admin_key else None
                    if channel_admins:
                        admin_list = [
                            aid.strip()
                            for aid in str(channel_admins).split(",")
                            if aid.strip()
                        ]
                        if user_id_str and user_id_str in admin_list:
                            return None

                        user = (
                            UserOper().get_by_name(self._username)
                            if self._username
                            else None
                        )
                        if user and user.is_superuser:
                            return None

                        return (
                            "抱歉，您没有执行此工具的权限。"
                            "只有渠道管理员或系统管理员才能执行工具操作。"
                            "如需执行工具，请联系渠道管理员将您的用户ID添加到渠道管理员列表中，"
                            "或联系系统管理员为您设置权限。"
                        )
                    else:
                        user = (
                            UserOper().get_by_name(self._username)
                            if self._username
                            else None
                        )
                        if user and user.is_superuser:
                            return None

                        if user_id_key:
                            config_user_id = config.config.get(user_id_key)
                            if config_user_id and str(config_user_id) == user_id_str:
                                return None

                        return (
                            "抱歉，您没有执行此工具的权限。"
                            "只有系统管理员才能执行工具操作。"
                            "如需执行工具，请联系系统管理员为您设置权限。"
                        )
        except Exception as e:
            logger.error(f"检查权限失败: {e}")

        return None

    async def send_tool_message(
        self, message: str, title: str = "", image: Optional[str] = None
    ):
        """
        发送工具消息
        """
        await ToolChain().async_post_message(
            Notification(
                channel=self._channel,
                source=self._source,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message,
                image=image,
            )
        )
