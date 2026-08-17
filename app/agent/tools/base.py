import asyncio
import json
import threading
from abc import ABCMeta, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, Protocol

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from app.agent.policy.sanitizer import (
    summarize_error,
    summarize_input,
    summarize_result,
)
from app.agent.tools.tags import ToolTag
from app.chain import ChainBase
from app.runtime.config import settings
from app.application.messaging.agent import matches_channel_admin
from app.runtime.extensions.service_registry import ServiceConfigHelper
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import NotificationChannel, MessageType

if TYPE_CHECKING:
    from app.agent.callback import StreamingHandler as _StreamingHandlerProtocol
else:
    class _StreamingHandlerProtocol(Protocol):
        """工具执行仅依赖的流式缓冲合同。"""

        @property
        def is_streaming(self) -> bool:
            """是否正在收集流式输出。"""
            ...

        @property
        def is_auto_flushing(self) -> bool:
            """是否由渠道编辑能力自动刷新缓冲。"""
            ...

        @property
        def last_buffer_char(self) -> str:
            """返回缓冲区最后一个字符。"""
            ...

        def emit(self, token: str) -> str:
            """追加流式文本并返回实际追加内容。"""
            ...

        async def take(self) -> str:
            """取出并清空当前缓冲内容。"""
            ...

        def record_tool_call(
            self,
            tool_name: str,
            tool_message: Optional[str] = None,
            tool_kwargs: Optional[dict[str, Any]] = None,
        ) -> None:
            """记录一次待汇总的工具调用。"""
            ...



def __getattr__(name: str) -> Any:
    """显式访问历史 StreamingHandler 符号时返回 canonical 实现。"""
    if name == "StreamingHandler":
        from app.agent.callback import StreamingHandler

        return StreamingHandler
    raise AttributeError(f"module 'app.agent.tools.base' has no attribute {name!r}")


class ToolChain(ChainBase):
    pass


# 单个工具结果的兜底上限。各工具仍应优先在自身逻辑中分页或摘要化；
# 这里用于拦截遗漏路径，避免超大结果直接进入模型上下文。
DEFAULT_TOOL_RESULT_MAX_CHARS = 64 * 1024


def serialize_tool_result_for_agent(result: Any) -> str:
    """将工具返回值稳定转换为 Agent 可消费的字符串。"""
    if isinstance(result, str):
        return result
    if isinstance(result, (int, float)):
        return str(result)
    try:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning(
            f"工具结果转换为JSON失败: {summarize_error(e)}, 使用字符串表示"
        )
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

    def _dump_preview(preview: str) -> str:
        """序列化截断结果，并让 returned_chars 与实际预览保持一致。"""
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

    # JSON 会转义换行、引号和反斜杠，预览本身等于上限时，最终返回值仍可能
    # 明显超限。通过二分查找预留包装开销，确保进入模型的最终字符串是硬上限。
    low = 0
    high = min(len(formatted_result), max_chars)
    best_result = _dump_preview("")
    while low <= high:
        middle = (low + high) // 2
        candidate = _dump_preview(formatted_result[:middle])
        if len(candidate) <= max_chars:
            best_result = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best_result


# 将常见的阻塞调用按能力域拆分到独立线程池，避免外部慢 IO 抢占同一批 worker。
_BLOCKING_BUCKET_LIMITS = {
    "command": 4,
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
    "web": 2,
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


def shutdown_blocking_executors(*, wait: bool = True, cancel_futures: bool = False) -> None:
    """关闭 Agent 工具阻塞线程池，释放长期运行进程或测试环境中的 worker。"""
    with _blocking_executor_lock:
        executors = list(_blocking_executors.values())
        _blocking_executors.clear()

    for executor in executors:
        executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class ToolExecutionTimeoutError(TimeoutError):
    """Agent 工具执行超时异常。"""


def _get_tool_timeout_seconds() -> Optional[float]:
    """读取工具执行超时时间，配置为 0 或负数时表示不限制。"""
    try:
        timeout = float(settings.LLM_TOOL_TIMEOUT or 0)
    except (TypeError, ValueError):
        timeout = 0
    return timeout if timeout > 0 else None


async def run_agent_blocking(
        bucket: str, func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """
    在受控线程池中运行阻塞型同步代码。

    调用方被取消时不会提前释放并发名额，避免底层阻塞调用仍在运行时继续接纳
    新任务，把同一类慢 IO 的线程池持续打满。
    """
    bucket_name = bucket if bucket in _BLOCKING_BUCKET_LIMITS else "default"
    semaphore = _blocking_semaphores[bucket_name]
    bound_call = partial(func, *args, **kwargs)
    loop = asyncio.get_running_loop()

    await semaphore.acquire()
    try:
        future = _get_blocking_executor(bucket_name).submit(bound_call)
    except Exception:
        semaphore.release()
        raise

    def _release_semaphore(_future) -> None:
        try:
            _future.exception()
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(semaphore.release)
        except RuntimeError:
            pass

    future.add_done_callback(_release_semaphore)
    return await asyncio.shield(asyncio.wrap_future(future, loop=loop))


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
    _stream_handler: Optional[_StreamingHandlerProtocol] = PrivateAttr(default=None)
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
            # 工具被权限门禁拦截时，模型在调用前可能已输出一段引导文本，且这里
            # 不会产生工具消息或统计摘要；补一个换行分隔符，避免随后的失败说明
            # 与引导文本直接连在一起。
            self._ensure_tool_boundary_separator()
            return permission_result

        # 获取工具执行提示消息
        tool_message = self.get_tool_message(**kwargs)

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

        logger.debug(
            f"Executing tool {self.name} with input summary: {summarize_input(kwargs)}"
        )

        # 执行具体工具逻辑
        try:
            result = await self.run_with_timeout(**kwargs)
            
            logger.info(
                f"Agent工具 {self.name} 执行完成，"
                f"结果摘要: {summarize_result(result)}"
            )
            
        except ToolExecutionTimeoutError as e:
            error_message = summarize_error(e)
            logger.warning(error_message)
            result = error_message
        except Exception as e:
            error_message = f"工具执行异常: {summarize_error(e)}"
            logger.error(f"Tool {self.name} execution failed: {summarize_error(e)}")
            result = error_message

        return format_tool_result_for_agent(
            result, tool_name=self.name, max_chars=self.result_max_chars
        )

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """
        获取工具执行时的友好提示消息。

        子类可以重写此方法，根据实际参数生成个性化的提示消息。
        Args:
            **kwargs: 工具的所有参数

        Returns:
            str: 友好的提示消息
        """
        return None

    @abstractmethod
    async def run(self, **kwargs) -> str:
        """子类实现具体的工具执行逻辑"""
        raise NotImplementedError

    async def run_with_timeout(self, **kwargs) -> str:
        """按系统配置限制单个工具调用的最长执行时间。"""
        timeout = _get_tool_timeout_seconds()
        if not timeout:
            return await self.run(**kwargs)
        try:
            return await asyncio.wait_for(self.run(**kwargs), timeout=timeout)
        except asyncio.TimeoutError as err:
            raise ToolExecutionTimeoutError(
                f"工具 {self.name} 执行超时（超过 {timeout:g} 秒），已停止等待结果。"
            ) from err

    @staticmethod
    async def run_blocking(
            bucket: str, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """
        在受控线程池中运行阻塞型同步代码，避免拖住 FastAPI 主事件循环。
        """
        return await run_agent_blocking(bucket, func, *args, **kwargs)

    def set_message_attr(self, channel: str, source: str, username: str):
        """
        设置消息属性
        """
        self._channel = channel
        self._source = source
        self._username = username

    def set_stream_handler(
        self, stream_handler: Optional[_StreamingHandlerProtocol]
    ) -> None:
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

    def _ensure_tool_boundary_separator(self) -> None:
        """
        在流式缓冲中为工具边界补一个换行分隔符。

        工具被权限门禁等前置检查拦截时不产生工具消息或统计摘要，若模型在调用前
        已输出文本，后续内容会直接粘在前文后面；这里保证缓冲以换行结尾，让工具
        前后的内容分行展示。缓冲为空或已以换行结尾时无需处理。
        """
        if (
            self._stream_handler
            and self._stream_handler.is_streaming
            and self._stream_handler.last_buffer_char not in ("", "\n")
        ):
            self._stream_handler.emit("\n")

    async def is_admin_user(self) -> bool:
        """
        判断当前工具调用者是否拥有管理员级权限。

        :return: 当前调用者是系统管理员、渠道管理员或显式管理员上下文时返回 True
        """
        if "is_admin" in self._agent_context:
            return self._agent_context.get("is_admin") is True

        if not self._channel or not self._source:
            return False

        return await self._has_channel_admin_permission()

    @staticmethod
    def _resolve_local_path(path: str) -> Path:
        """
        解析本地路径并展开符号链接。

        :param path: 用户传入的本地文件或目录路径
        :return: 规范化后的绝对路径
        """
        return Path(path).expanduser().resolve(strict=False)

    @staticmethod
    def _is_path_relative_to(path: Path, root: Path) -> bool:
        """
        判断路径是否位于指定目录内。

        :param path: 待检查路径
        :param root: 允许访问的根目录
        :return: 路径在根目录内或等于根目录时返回 True
        """
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _get_non_admin_local_file_roots(cls) -> list[Path]:
        """
        获取普通用户可访问的本地文件根目录。

        :return: 普通用户允许读写的本地目录列表
        """
        roots = [
            settings.CONFIG_PATH / "agent",
        ]
        resolved_roots = []
        for root in roots:
            resolved_root = cls._resolve_local_path(str(root))
            if resolved_root not in resolved_roots:
                resolved_roots.append(resolved_root)
        return resolved_roots

    async def _check_local_file_access(
        self, path: str, operation: str = "访问"
    ) -> tuple[Optional[Path], Optional[str]]:
        """
        检查当前用户是否可访问指定本地路径。

        :param path: 用户传入的本地文件或目录路径
        :param operation: 当前操作名称，用于生成拒绝提示
        :return: 解析后的路径和拒绝原因；拒绝原因为空表示允许访问
        """
        if not path:
            return None, "错误：路径不能为空"

        resolved_path = self._resolve_local_path(path)
        if await self.is_admin_user():
            return resolved_path, None

        allowed_roots = self._get_non_admin_local_file_roots()
        if any(
            self._is_path_relative_to(resolved_path, root)
            for root in allowed_roots
        ):
            return resolved_path, None

        allowed_text = "、".join(str(root) for root in allowed_roots)
        return (
            resolved_path,
            f"抱歉，普通用户只能{operation}Agent配置目录内的文件或目录：{allowed_text}",
        )

    async def _check_local_storage_access(
        self,
        path: str,
        storage: Optional[str] = "local",
        operation: str = "访问",
    ) -> tuple[Optional[Path], Optional[str]]:
        """
        检查当前用户是否可访问指定存储路径。

        :param path: 用户传入的文件或目录路径
        :param storage: 存储类型，普通用户只允许 local
        :param operation: 当前操作名称，用于生成拒绝提示
        :return: 本地存储时返回解析后的路径和拒绝原因；远程存储无本地路径
        """
        if (storage or "local") != "local":
            if await self.is_admin_user():
                return None, None
            return (
                None,
                f"抱歉，普通用户只能{operation}本地Agent配置目录，不能访问远程存储。",
            )

        return await self._check_local_file_access(path=path, operation=operation)

    async def _check_permission(self) -> Optional[str]:
        """
        检查管理员工具权限。

        Agent 共享上下文中的显式管理员事实优先；没有该事实的旧调用才按渠道
        管理员名单回查，并保留无消息渠道内部调用的兼容行为。
        """
        if not self._require_admin:
            return None

        if await self.is_admin_user():
            return None

        if "is_admin" not in self._agent_context and (
            not self._channel or not self._source
        ):
            return None

        return (
            "抱歉，您没有执行此工具的权限。"
            "只有渠道管理员或系统管理员才能执行工具操作。"
            "如需执行工具，请联系管理员将您的用户ID添加到渠道管理员列表中（设定 -> 通知 -> 对应渠道配置 -> 管理员名单），"
            "或联系系统管理员为您设置管理员权限。"
        )

    async def _has_channel_admin_permission(self) -> bool:
        """
        检查当前消息渠道身份是否具备管理员权限。

        :return: 当前渠道稳定用户 ID 位于显式管理员名单或等于渠道主ID时返回 True
        """
        if not self._channel or not self._source:
            return False

        user_id_str = str(self._user_id) if self._user_id else None

        try:
            channel = NotificationChannel(self._channel)
        except ValueError:
            return False

        try:
            configs = ServiceConfigHelper.get_notification_configs()
            for config in configs:
                if config.name == self._source and config.config:
                    return matches_channel_admin(
                        channel,
                        config.config,
                        user_id_str,
                    )
        except Exception as e:
            logger.error(f"检查权限失败: {summarize_error(e)}")

        return False

    async def send_message(self, message: Message) -> None:
        """
        发送工具消息。

        WebAgent 渠道没有后端模块实例，前端流式面板通过 Agent 上下文中的
        回调直接接收消息；无渠道的后台任务清空渠道侧定位信息后交由消息链广播，
        其它渠道继续走统一消息链。
        """
        callback = self._agent_context.get("message_callback")
        if (
            self._channel == NotificationChannel.WebAgent.value
            and callable(callback)
        ):
            callback(message)
            return

        if not self._channel or not self._source:
            message = message.model_copy(
                update={
                    "channel": None,
                    "source": None,
                    "userid": None,
                    "username": message.username
                    or self._username
                    or settings.SUPERUSER,
                    "original_message_id": None,
                    "original_chat_id": None,
                }
            )
        elif not message.original_chat_id:
            # 工具回调消息默认回填当前会话的原会话 ID，
            # 保证群聊 @ 机器人时按钮选择、消息发送等交互消息回复到原群，而不是私聊窗口。
            original_chat_id = str(
                self._agent_context.get("original_chat_id") or ""
            ).strip() or None
            if original_chat_id:
                message = message.model_copy(
                    update={"original_chat_id": original_chat_id}
                )

        await ToolChain().async_post_message(message)

    async def send_tool_message(
        self, message: str, title: str = "", image: Optional[str] = None
    ) -> None:
        """
        发送工具消息
        """
        await self.send_message(
            Message(
                channel=self._channel,
                source=self._source,
                mtype=MessageType.Agent,
                userid=self._user_id,
                username=self._username,
                title=title,
                text=message,
                image=image,
                save_history=False,
            )
        )


# 普通导入保持 callback 冷态；显式导入或历史星号导入仍解析真实类。
__all__ = sorted(
    {name for name in globals() if not name.startswith("_")}
    | {"StreamingHandler"}
)
