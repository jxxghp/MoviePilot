import asyncio
import json
import threading
from abc import ABCMeta, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, ClassVar, Optional

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from app.agent import StreamingHandler
from app.agent.tools.tags import ToolTag
from app.chain import ChainBase
from app.core.config import settings
from app.db.user_oper import UserOper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import Notification
from app.schemas.types import MessageChannel, NotificationType


class ToolChain(ChainBase):
    pass


# 单个工具结果的兜底上限。各工具仍应优先在自身逻辑中分页或摘要化；
# 这里用于拦截遗漏路径，避免超大结果直接进入模型上下文。
DEFAULT_TOOL_RESULT_MAX_CHARS = 64 * 1024
MIN_TOOL_RESULT_PREVIEW_CHARS = 512


def serialize_tool_result_for_agent(result: Any) -> str:
    """将工具返回值稳定转换为 Agent 可消费的字符串。"""
    if isinstance(result, str):
        return result
    if isinstance(result, (int, float)):
        return str(result)
    try:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning(f"工具结果转换为JSON失败: {e}, 使用字符串表示")
        return str(result)


def format_tool_result_for_agent(
    result: Any,
    *,
    tool_name: Optional[str] = None,
    max_chars: Optional[int] = DEFAULT_TOOL_RESULT_MAX_CHARS,
) -> str:
    """
    统一格式化工具结果，并在超长时返回结构化预览。

    具体工具可以通过 `result_max_chars` 覆盖上限；传入 None 或 <=0 表示不截断。
    """
    formatted_result = serialize_tool_result_for_agent(result)
    if not max_chars or max_chars <= 0 or len(formatted_result) <= max_chars:
        return formatted_result

    preview_limit = max(MIN_TOOL_RESULT_PREVIEW_CHARS, max_chars)
    preview = formatted_result[:preview_limit]
    payload = {
        "tool_result_truncated": True,
        "tool_name": tool_name,
        "total_chars": len(formatted_result),
        "returned_chars": len(preview),
        "content_preview": preview,
        "message": (
            f"工具返回内容超过 {max_chars} 字符，已截断为预览；"
            "请使用更精确的筛选条件、分页参数或专用查询参数继续获取。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# 将常见的阻塞调用按能力域拆分到独立线程池，避免外部慢 IO 抢占同一批 worker。
_BLOCKING_BUCKET_LIMITS = {
    "default": 4,
    "config": 2,
    "db": 4,
    "downloader": 4,
    "mediaserver": 4,
    "plugin": 2,
    "rule": 2,
    "site": 4,
    "storage": 4,
    "subscribe": 2,
    "workflow": 2,
}
_blocking_semaphores = {
    bucket: asyncio.Semaphore(limit)
    for bucket, limit in _BLOCKING_BUCKET_LIMITS.items()
}
_blocking_executors: dict[str, ThreadPoolExecutor] = {}
_blocking_executor_lock = threading.Lock()


def _get_blocking_executor(bucket: str) -> ThreadPoolExecutor:
    """按桶懒加载线程池，避免在导入阶段创建过多 worker。"""
    with _blocking_executor_lock:
        executor = _blocking_executors.get(bucket)
        if executor:
            return executor

        limit = _BLOCKING_BUCKET_LIMITS[bucket]
        executor = ThreadPoolExecutor(
            max_workers=limit,
            thread_name_prefix=f"agent-tool-{bucket}",
        )
        _blocking_executors[bucket] = executor
        return executor


class MoviePilotTool(BaseTool, metaclass=ABCMeta):
    """
    MoviePilot专用工具基类（LangChain v1 / langchain_core）
    """

    result_max_chars: ClassVar[Optional[int]] = DEFAULT_TOOL_RESULT_MAX_CHARS

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
        # require_admin 在各工具子类以 pydantic 字段声明，pydantic v2 不在类对象上暴露字段值
        # （getattr(cls, ...) 取不到），必须经实例读取——super().__init__() 已按字段默认填充实例；
        # getattr 兜底兼容未声明该字段的工具，缺省按非管理员（False）处理。
        self._require_admin = getattr(self, "require_admin", False)
        self.tags = self._build_tool_tags()

    @staticmethod
    def _normalize_tag_values(tags: Optional[Any]) -> set[str]:
        """规范化 LangChain 工具标签。"""
        if not tags:
            return set()
        if isinstance(tags, (str, ToolTag)):
            tags = [tags]
        normalized_tags = set()
        for tag in tags:
            if isinstance(tag, ToolTag):
                normalized_tags.add(tag.value)
            elif tag:
                normalized_tags.add(str(tag))
        return normalized_tags

    def _build_tool_tags(self) -> list[str]:
        """规范化工具实现中显式声明的标签。"""
        explicit_tags = self._normalize_tag_values(getattr(self, "tags", None))
        return sorted(explicit_tags | {ToolTag.AgentTool.value})

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

        # 发送工具执行过程消息（流式传输且非最后终结工具时）
        if self._stream_handler and self._stream_handler.is_streaming and not self.return_direct:
            if settings.AI_AGENT_VERBOSE:
                if self._stream_handler.is_auto_flushing:
                    # 渠道支持编辑：工具消息追加到 buffer，由定时刷新推送
                    if tool_message:
                        self._stream_handler.emit(f"\n\n⚙️ => {tool_message}\n\n")
                else:
                    allow_dispatch_without_context = self._agent_context.get(
                        "should_dispatch_reply", False
                    )
                    if self._channel and self._source:
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
                    elif allow_dispatch_without_context:
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
                        # 后台 capture 流程没有渠道上下文，不能把工具提示回灌到默认通知渠道。
                        self._stream_handler.record_tool_call(
                            tool_name=self.name,
                            tool_message=tool_message,
                            tool_kwargs=kwargs,
                        )
            else:
                # 非VERBOSE：不逐条回显工具调用，转为在下一段文本前补一句聚合摘要
                self._stream_handler.record_tool_call(
                    tool_name=self.name,
                    tool_message=tool_message,
                    tool_kwargs=kwargs,
                )
        else:
            # 未启用流式传输，不发送任何工具消息内容
            pass

        logger.debug(f"Executing tool {self.name} with args: {kwargs}")

        # 执行具体工具逻辑
        try:
            result = await self.run(**kwargs)
            
            # 记录工具执行结果摘要日志
            str_result = serialize_tool_result_for_agent(result)
            if len(str_result) > 500:
                summary = str_result[:500] + f"...(已截断，总长度: {len(str_result)})"
            else:
                summary = str_result
            logger.info(f"Agent工具 {self.name} 执行完成，结果摘要: {summary}")
            
        except Exception as e:
            error_message = f"工具执行异常 ({type(e).__name__}): {str(e)}"
            logger.error(f"Tool {self.name} execution failed: {e}", exc_info=True)
            result = error_message

        return format_tool_result_for_agent(
            result, tool_name=self.name, max_chars=self.result_max_chars
        )

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
        explanation = kwargs.get("explanation")
        return str(explanation) if explanation else None

    @abstractmethod
    async def run(self, **kwargs) -> str:
        """子类实现具体的工具执行逻辑"""
        raise NotImplementedError

    @staticmethod
    async def run_blocking(
            bucket: str, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """
        在受控线程池中运行阻塞型同步代码，避免拖住 FastAPI 主事件循环。
        """
        bucket_name = bucket if bucket in _BLOCKING_BUCKET_LIMITS else "default"
        semaphore = _blocking_semaphores[bucket_name]
        bound_call = partial(func, *args, **kwargs)

        async with semaphore:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _get_blocking_executor(bucket_name), bound_call
            )

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
        # 空 dict 也是合法共享上下文；不能用 ``or {}``，否则每个工具会拿到
        # 独立的新 dict，跨工具状态（例如质量门槛拒绝标记）无法传播。
        self._agent_context = {} if agent_context is None else agent_context

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

        # 渠道配置来自 SystemConfigOper 内存缓存，可以直接读取；
        # 只有用户信息需要走异步数据库查询。
        user_id_str = str(self._user_id) if self._user_id else None

        channel_type_map = {
            MessageChannel.Telegram: "telegram",
            MessageChannel.Discord: "discord",
            MessageChannel.Wechat: "wechat",
            MessageChannel.Feishu: "feishu",
            MessageChannel.WechatClawBot: "wechatclawbot",
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
            "feishu": "FEISHU_ADMINS",
            "wechatclawbot": "WECHATCLAWBOT_ADMINS",
            "slack": "SLACK_ADMINS",
            "vocechat": "VOCECHAT_ADMINS",
            "synologychat": "SYNOLOGYCHAT_ADMINS",
            "qqbot": "QQBOT_ADMINS",
        }

        user_id_key_map = {
            "telegram": "TELEGRAM_CHAT_ID",
            "vocechat": "VOCECHAT_CHANNEL_ID",
            "wechat": "WECHAT_BOT_CHAT_ID",
            "feishu": "FEISHU_OPEN_ID",
            "wechatclawbot": "WECHATCLAWBOT_DEFAULT_TARGET",
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
                            await UserOper().async_get_by_name(self._username)
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
                            await UserOper().async_get_by_name(self._username)
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
    ) -> None:
        """
        发送工具消息
        """
        await ToolChain().async_post_message(
            Notification(
                channel=self._channel,
                source=self._source,
                mtype=NotificationType.Agent,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message,
                image=image,
            )
        )
