import asyncio
import re
import threading
from typing import Any, Optional, Tuple

from app.agent.policy.sanitizer import sanitize_for_host
from app.chain.base import ChainBase
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.message import Message, MessageResponse
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageType, NotificationChannel


class _StreamChain(ChainBase):
    pass


_PATCH_FILE_HEADER_PATTERN = re.compile(r"\*\*\* (?:Add|Update|Delete) File:\s*(\S+)")


def _extract_first_patch_path(patch: Optional[str]) -> Optional[str]:
    """从补丁文本中提取首个文件路径，作为流式消息展示目标。"""
    if not patch:
        return None
    match = _PATCH_FILE_HEADER_PATTERN.search(patch)
    return match.group(1) if match else None


class StreamingHandler:
    """
    流式Token缓冲管理器

    负责从 LLM 流式 token 中积累文本，并在支持消息编辑的渠道上实时推送给用户。

    工作流程：
    1. Agent开始处理时调用 start_streaming()，检查渠道能力并启动定时刷新
    2. LLM 产生 token 时调用 emit() 积累到缓冲区
    3. 定时器周期性调用 _flush()：
       - 第一次有内容时发送新消息（通过 send_direct_message 获取 message_id）
       - 后续有新内容时编辑同一条消息（通过 edit_message）
       - 当消息长度接近渠道限制时，冻结当前消息并发送新消息继续输出
    4. 工具调用时：
       - 流式渠道：工具消息直接 emit() 追加到 buffer，与 Agent 文字合并为同一条流式消息
       - 非流式渠道：调用 take() 取出已积累的文字，与工具消息合并独立发送
    5. Agent最终完成时调用 stop_streaming()：执行最后一次刷新，
       返回是否已通过流式发送完所有内容（调用方据此决定是否还需额外发送）
    """

    # 流式输出的刷新间隔（秒）
    FLUSH_INTERVAL = 0.3

    def __init__(self):
        self._lock = threading.Lock()
        self._buffer = ""
        # 流式输出相关状态
        self._streaming_enabled = False
        self._flush_task: Optional[asyncio.Task] = None
        self._streaming_lifecycle_lock = asyncio.Lock()
        # 当前消息的发送信息（用于编辑消息）
        self._message_response: Optional[MessageResponse] = None
        # 已发送给用户的文本（用于追踪增量）
        self._sent_text = ""
        # 当前消息的起始偏移量（buffer 中属于当前消息的起始位置）
        self._msg_start_offset = 0
        # 当前渠道的单条消息最大长度（0 表示不限制）
        self._max_message_length = 0
        # 消息发送所需的上下文信息
        self._channel: Optional[str] = None
        self._source: Optional[str] = None
        self._user_id: Optional[str] = None
        self._username: Optional[str] = None
        self._original_message_id: Optional[str] = None
        self._original_chat_id: Optional[str] = None
        self._title: str = ""
        self._allow_dispatch_without_context = False
        # 非啰嗦模式下的待输出工具统计，等下一段文本到来时再统一补一句摘要
        self._pending_tool_stats: dict[str, dict[str, Any]] = {}
        # 本轮已写入缓冲区的工具展示行，供 Telegram 富文本渲染时做区分样式
        self._tool_summaries: set[str] = set()

    def set_dispatch_policy(self, allow_dispatch_without_context: bool = False) -> None:
        """
        设置在缺少渠道上下文时是否仍允许向默认通知渠道分发消息。
        后台 DISPATCH 任务允许，CAPTURE_ONLY 必须禁止。
        """
        self._allow_dispatch_without_context = allow_dispatch_without_context

    def emit(self, token: str) -> str:
        """
        接收 LLM 流式 token，积累到缓冲区。
        如果存在待输出的工具统计，则会先补上一句摘要再追加 token。
        """
        with self._lock:
            emitted = token or ""

            if self._pending_tool_stats:
                summary = self._consume_pending_tool_summary_locked()
                if summary:
                    if emitted:
                        emitted = f"{summary}{emitted.lstrip(chr(10))}"
                    else:
                        emitted = summary

            # 如果存量消息结束是两个换行，则去掉新消息前面的换行，避免过多空行
            if self._buffer.endswith("\n\n") and emitted.startswith("\n"):
                emitted = emitted.lstrip("\n")
            self._buffer += emitted
            return emitted

    def emit_tool_message(self, message: str) -> str:
        """
        将啰嗦模式的逐条工具提示写入缓冲区，并登记为独立展示行。

        Telegram Rich Markdown 依赖登记结果把工具提示渲染成引用块；
        其他渠道仍消费相同的纯文本缓冲内容。
        """
        normalized_message = str(message or "").strip()
        if not normalized_message:
            return ""

        tool_message = f"⚙️ => {normalized_message}"
        with self._lock:
            self._tool_summaries.update(
                line for line in tool_message.splitlines() if line
            )
        return self.emit(f"\n\n{tool_message}\n\n")

    def report_tool_call(
        self,
        tool_name: str,
        tool_message: Optional[str] = None,
        tool_kwargs: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        按 Agent 详细模式逐条展示工具调用，或登记为延迟汇总。

        中间件私有工具不经过 ``MoviePilotTool._arun``，必须通过本入口复用
        相同的详细模式语义，避免无条件调用 ``record_tool_call`` 后只显示汇总。
        """
        safe_message = sanitize_for_host(tool_message) if tool_message else tool_message
        if get_runtime_setting("AI_AGENT_VERBOSE") and safe_message:
            return self.emit_tool_message(str(safe_message))
        self.record_tool_call(
            tool_name=tool_name,
            tool_message=str(safe_message) if safe_message else None,
            tool_kwargs=tool_kwargs,
        )
        return ""

    async def take(self) -> str:
        """
        获取当前已积累的消息内容，获取后清空缓冲区。

        用于非流式渠道：工具调用前取出 Agent 已产出的文字，
        与工具提示合并后独立发送。

        注意：流式渠道不调用此方法，工具消息直接 emit 到 buffer 中。
        """
        self.flush_pending_tool_summary()

        with self._lock:
            if not self._buffer:
                return ""
            message = self._buffer
            logger.info(f"Agent消息: {message}")
            self._buffer = ""
            return message

    def clear(self):
        """
        清空缓冲区（不返回内容）
        """
        with self._lock:
            self._buffer = ""
            self._sent_text = ""
            self._message_response = None
            self._msg_start_offset = 0
            self._pending_tool_stats = {}
            self._tool_summaries = set()

    def reset(self):
        """
        重置缓冲区，清空已发送的文本从头更新，但保持消息编辑能力。

        与 clear 的区别：
        - clear：完全重置所有状态，后续会开新消息
        - reset：只清空buffer，保留消息编辑状态，后续继续编辑同一条消息
        """
        with self._lock:
            self._buffer = ""
            self._sent_text = ""
            self._msg_start_offset = 0
            self._pending_tool_stats = {}
            self._tool_summaries = set()

    async def start_streaming(
        self,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        original_message_id: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        title: str = "",
    ):
        """串行启动流式输出，禁止新一轮覆盖尚未结束的刷新 owner。"""
        async with self._streaming_lifecycle_lock:
            await self._start_streaming(
                channel=channel,
                source=source,
                user_id=user_id,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                title=title,
            )

    async def _start_streaming(
        self,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        original_message_id: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        title: str = "",
    ):
        """
        启动流式输出。
        始终标记为流式状态（用于 buffer 收集 token），
        但只有渠道支持消息编辑时才启动定时刷新任务（实时推送给用户）。
        :param channel: 消息渠道
        :param source: 消息来源
        :param user_id: 用户ID
        :param username: 用户名
        :param title: 消息标题
        :param original_message_id: 原始消息ID（如果是回复消息）
        :param original_chat_id: 原始聊天ID（如果是回复消息）
        """
        if self._flush_task is not None:
            self._streaming_enabled = False
            await self._cancel_flush_task()

        self._channel = channel
        self._source = source
        self._user_id = user_id
        self._username = username
        self._original_message_id = original_message_id
        self._original_chat_id = original_chat_id
        self._title = title

        self._streaming_enabled = True
        self._sent_text = ""
        self._message_response = None
        self._msg_start_offset = 0
        self._pending_tool_stats = {}
        self._tool_summaries = set()

        # 检查渠道是否支持消息编辑，不支持则仅收集 token 到 buffer，不实时推送
        if not self._can_stream():
            logger.debug(f"渠道 {channel} 不支持消息编辑，仅启用 buffer 收集模式")
            return

        # 从渠道能力中获取单条消息最大长度
        try:
            channel_enum = NotificationChannel(self._channel)
            self._max_message_length = ChannelCapabilityManager.get_max_message_length(channel_enum)
        except (ValueError, KeyError):
            self._max_message_length = 0

        # 启动异步定时刷新任务
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.debug("流式输出已启动")

    async def stop_streaming(self) -> Tuple[bool, str]:
        """串行停止流式输出，并等待本轮刷新与最终消息收口。"""
        async with self._streaming_lifecycle_lock:
            return await self._stop_streaming()

    async def _stop_streaming(self) -> Tuple[bool, str]:
        """
        停止流式输出。执行最后一次刷新确保所有内容都已发送。
        :return: (all_sent, final_text)
                 all_sent: 是否已经通过流式编辑将最终完整内容发送给了用户
                           （True 表示调用方无需再额外发送消息）
                 final_text: 流式发送的完整文本内容（用于调用方保存消息记录）
        """
        if not self._streaming_enabled:
            return False, ""

        self._streaming_enabled = False

        # 取消定时任务
        await self._cancel_flush_task()

        # 将未落地的工具统计补入缓冲区，避免流式结束时丢失这段执行信息
        self.flush_pending_tool_summary()

        # 执行最后一次刷新
        await self._flush()

        message_response = self._message_response
        if message_response:
            await run_in_threadpool(
                _StreamChain().finalize_message,
                message_response,
            )

        # 检查是否所有缓冲内容都已发送
        with self._lock:
            # 当前消息的文本 = buffer 中从 _msg_start_offset 开始的部分
            current_msg_text = self._buffer[self._msg_start_offset :]
            all_sent = self._message_response is not None and self._sent_text and current_msg_text == self._sent_text
            # 保留最终文本用于返回（返回完整 buffer 内容，包含所有分段消息）
            final_text = self._buffer if all_sent else ""
            # 重置状态
            self._sent_text = ""
            self._message_response = None
            self._msg_start_offset = 0
            self._pending_tool_stats = {}
            self._tool_summaries = set()
            if all_sent:
                # 所有内容已通过流式发送，清空缓冲区
                self._buffer = ""
            return all_sent, final_text

    def record_tool_call(
        self,
        tool_name: str,
        tool_message: Optional[str] = None,
        tool_kwargs: Optional[dict[str, Any]] = None,
    ):
        """
        记录一次工具调用，供非啰嗦模式下延迟汇总输出。
        """
        recorded_message = sanitize_for_host(tool_message) if tool_message else tool_message
        recorded_args = sanitize_for_host(tool_kwargs or {})
        if not isinstance(recorded_args, dict):
            recorded_args = {}
        category, target = self._classify_tool_call(
            tool_name=tool_name,
            tool_message=recorded_message,
            tool_kwargs=recorded_args,
        )
        target_values = []
        if isinstance(target, (list, tuple, set)):
            target_values = [item for item in target if item]
        elif target:
            target_values = [target]

        with self._lock:
            bucket = self._pending_tool_stats.setdefault(
                category,
                {
                    "count": 0,
                    "targets": set(),
                },
            )
            if category == "subagent" and target_values:
                bucket["count"] += len(target_values)
            else:
                bucket["count"] += 1
            for target_value in target_values:
                bucket["targets"].add(str(target_value))

    @staticmethod
    def _extract_subagent_targets(tool_kwargs: dict[str, Any]) -> list[str]:
        """提取子代理工具请求中的目标子代理类型。"""
        tasks = tool_kwargs.get("tasks")
        if not isinstance(tasks, list):
            subagent_type = tool_kwargs.get("subagent_type")
            return [str(subagent_type)] if subagent_type else []

        targets = []
        for task in tasks:
            if isinstance(task, dict):
                subagent_type = task.get("subagent_type")
            else:
                subagent_type = getattr(task, "subagent_type", None)
            if subagent_type:
                targets.append(str(subagent_type))
        return targets

    def flush_pending_tool_summary(self) -> str:
        """
        将待输出的工具统计摘要补入缓冲区，并返回本次新增的摘要文本。
        """
        with self._lock:
            summary = self._consume_pending_tool_summary_locked()
            if summary:
                self._buffer += summary
            return summary

    @staticmethod
    def _classify_tool_call(
        tool_name: str,
        tool_message: Optional[str],
        tool_kwargs: dict[str, Any],
    ) -> tuple[str, Optional[Any]]:
        tool_name = (tool_name or "").strip().lower()
        tool_message = (tool_message or "").strip()
        tool_message_lower = tool_message.lower()

        if tool_name in {"read_skill", "skill"}:
            return "skill", tool_kwargs.get("name")
        if tool_name == "query_activity_log":
            return "activity_log", tool_kwargs.get("keyword") or tool_kwargs.get("date")
        if tool_name == "subagent_task":
            return "subagent", StreamingHandler._extract_subagent_targets(tool_kwargs)
        if tool_name == "task":
            return "subagent", tool_kwargs.get("subagent_type")
        if tool_name == "read_file":
            return "file_read", tool_kwargs.get("file_path")
        if tool_name in {"write_file", "edit_file"}:
            return "file_write", tool_kwargs.get("file_path")
        if tool_name == "apply_patch":
            return "file_write", _extract_first_patch_path(tool_kwargs.get("patch"))
        if tool_name == "browse_webpage":
            return (
                "web_browse",
                tool_kwargs.get("url") or tool_kwargs.get("target_url") or tool_kwargs.get("path"),
            )
        if tool_name == "execute_command":
            return (
                "command",
                tool_kwargs.get("command") or tool_kwargs.get("session_id"),
            )
        if tool_name == "ask_user_choice":
            return "interaction", tool_kwargs.get("message")
        if tool_name == "moviepilot_api":
            operation_id = str(tool_kwargs.get("operation_id") or "")
            if operation_id in {"storage.settings", "storage.list"}:
                body = tool_kwargs.get("body")
                target = body.get("path") if isinstance(body, dict) else None
                return "directory", target
            if operation_id in {
                "media.search",
                "media.person.search",
                "search.torrents",
                "search.results",
                "recommendation.list",
            }:
                query = tool_kwargs.get("query")
                target = query.get("title") if isinstance(query, dict) else None
                return "search", target
            if operation_id.endswith((".list", ".get", ".exists", ".history", ".detail")) or operation_id in {
                "media.episode_schedule",
                "plugin.data",
                "filter.builtin",
                "filter.custom",
                "filter.groups",
                "site.userdata",
                "search.results",
            }:
                return "data_query", None
            return "action", None
        if tool_name == "agent_task":
            return ("data_query", None) if tool_kwargs.get("action") == "list" else ("action", None)
        if tool_name == "persona":
            return ("data_query", None) if tool_kwargs.get("action") == "list" else ("action", None)
        if tool_name.startswith("search_"):
            return (
                "search",
                tool_kwargs.get("query") or tool_kwargs.get("title") or tool_kwargs.get("keyword"),
            )
        if tool_name.startswith("query_") or tool_name.startswith("list_") or tool_name.startswith("get_"):
            return "data_query", None
        if tool_name.startswith(("add_", "update_", "delete_", "modify_", "run_")):
            return "action", None
        if tool_name in {
            "send_message",
            "send_local_file",
            "send_voice_message",
        }:
            return "action", None

        if "读取文件" in tool_message or "read file" in tool_message_lower:
            return "file_read", tool_kwargs.get("file_path")
        if (
            "写入文件" in tool_message
            or "编辑文件" in tool_message
            or "write file" in tool_message_lower
            or "edit file" in tool_message_lower
        ):
            return "file_write", tool_kwargs.get("file_path")
        if "目录" in tool_message or "directory" in tool_message_lower:
            return "directory", tool_kwargs.get("path")
        if "搜索" in tool_message or "search" in tool_message_lower:
            return (
                "search",
                tool_kwargs.get("query") or tool_kwargs.get("title") or tool_kwargs.get("keyword"),
            )
        if "网页" in tool_message or "browser" in tool_message_lower or "webpage" in tool_message_lower:
            return "web_browse", tool_kwargs.get("url")
        if "命令" in tool_message or "command" in tool_message_lower:
            return "command", tool_kwargs.get("command")

        return "tool", None

    def _consume_pending_tool_summary_locked(self) -> str:
        if not self._pending_tool_stats:
            return ""

        parts = []
        for category, bucket in self._pending_tool_stats.items():
            value = bucket["count"]
            if category in {"file_read", "file_write", "directory", "web_browse", "skill"} and bucket["targets"]:
                value = len(bucket["targets"])
            part = self._format_tool_stat(category, value)
            if part:
                parts.append(part)

        self._pending_tool_stats = {}
        if not parts:
            return ""

        summary = f"（{'，'.join(parts)}）"
        self._tool_summaries.add(summary)
        # 摘要前始终保证一个空行，让工具执行信息与正文分属不同段落，
        # 避免 Markdown 富文本把单个换行折叠成同一段落内的软换行
        visible_buffer = self._buffer.rstrip(" \t")
        trailing_newlines = len(visible_buffer) - len(visible_buffer.rstrip("\n"))
        prefix = ""
        if visible_buffer.strip():
            prefix = "\n" * max(2 - trailing_newlines, 0)
        return f"{prefix}{summary}\n\n"

    @staticmethod
    def _format_tool_stat(category: str, count: int) -> str:
        if count <= 0:
            return ""

        if category == "search":
            return f"执行了 {count} 次搜索"
        if category == "file_read":
            return f"读取了 {count} 个文件"
        if category == "file_write":
            return f"修改了 {count} 个文件"
        if category == "directory":
            return f"查看了 {count} 个目录"
        if category == "web_browse":
            return f"浏览了 {count} 个网页"
        if category == "command":
            return f"执行了 {count} 条命令"
        if category == "data_query":
            return f"查询了 {count} 次数据"
        if category == "skill":
            return f"查询了 {count} 个技能说明"
        if category == "activity_log":
            return f"查询了 {count} 次活动日志"
        if category == "action":
            return f"执行了 {count} 次操作"
        if category == "interaction":
            return f"发起了 {count} 次交互"
        if category == "subagent":
            return f"已调用 {count} 个子代理"
        return f"调用了 {count} 次工具"

    def _can_stream(self) -> bool:
        """
        检查当前渠道是否支持流式输出（消息编辑）
        """
        if not self._channel:
            return False
        try:
            channel_enum = NotificationChannel(self._channel)
            return ChannelCapabilityManager.supports_capability(channel_enum, ChannelCapability.MESSAGE_EDITING)
        except (ValueError, KeyError):
            return False

    def _get_rich_message(self, text: str) -> Optional[str]:
        """
        为 Telegram 流式消息返回 Rich Markdown，其他渠道继续使用原有格式。
        """
        if self._channel != NotificationChannel.Telegram.value:
            return None
        return self._quote_tool_summary_lines(text)

    def _quote_tool_summary_lines(self, text: str) -> str:
        """
        将缓冲区中的工具展示行转换为 Markdown 引用块。

        富文本会把普通段落间的空行折叠成紧凑排版，引用块作为独立 block 类型
        渲染，保证工具执行信息在 Telegram 上始终与正文有可辨识的视觉分隔。
        """
        if not self._tool_summaries or not text:
            return text
        return "\n".join(f"> {line}" if line in self._tool_summaries else line for line in text.split("\n"))

    async def _flush_loop(self):
        """
        定时刷新循环，定期将缓冲区内容发送/编辑到用户
        """
        try:
            while self._streaming_enabled:
                await asyncio.sleep(self.FLUSH_INTERVAL)
                if self._streaming_enabled:
                    await self._flush()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"流式刷新异常: {e}")

    async def _cancel_flush_task(self):
        """
        停止当前的定时刷新任务。

        停止流式输出时，刷新任务可能已经在线程池里发出了首条消息。
        这里先等待该轮刷新自然完成，确保 message_id 等返回信息能落回本地状态；
        否则最终刷新会误以为尚未发送过消息，从而再次发送一条新消息。
        """
        current_task = asyncio.current_task()
        if self._flush_task and not self._flush_task.done() and self._flush_task is not current_task:
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        self._flush_task = None

    async def _flush(self):
        """
        将当前缓冲区内容刷新到用户消息
        - 如果还没有发送过消息，先发送一条新消息并记录message_id
        - 如果已经发送过消息，编辑该消息为最新的完整内容
        - 如果当前消息内容超过长度限制，冻结当前消息并发送新消息继续输出
        """
        with self._lock:
            # 当前消息的文本 = buffer 中从 _msg_start_offset 开始的部分
            current_text = self._buffer[self._msg_start_offset :]
            if not current_text or current_text == self._sent_text:
                # 没有新内容需要刷新
                return
            if (not self._channel or not self._source) and not self._allow_dispatch_without_context:
                logger.debug("流式输出缺少渠道上下文，当前模式禁止外发消息")
                return

        chain = _StreamChain()

        try:
            if self._message_response is None:
                # 第一次发送：发送新消息并获取 message_id
                response = await run_in_threadpool(
                    chain.send_direct_message,
                    Message(
                        channel=self._channel,
                        source=self._source,
                        mtype=MessageType.Agent,
                        userid=self._user_id,
                        username=self._username,
                        original_message_id=self._original_message_id,
                        original_chat_id=self._original_chat_id,
                        title=self._title,
                        text=current_text,
                        rich_message=self._get_rich_message(current_text),
                        save_history=False,
                    ),
                )
                if response and response.success and response.message_id:
                    self._message_response = response
                    with self._lock:
                        self._sent_text = current_text
                    logger.debug(f"流式输出初始消息已发送: message_id={response.message_id}")
                else:
                    logger.debug("流式输出初始消息发送失败或未返回message_id，降级为非流式输出")
                    self._streaming_enabled = False
            else:
                # 检查当前消息内容是否超过长度限制
                if self._max_message_length and len(current_text) > self._max_message_length:
                    # 消息过长，冻结当前消息（保持最后一次成功编辑的内容）
                    # 将 offset 移动到已发送文本之后，开启新消息
                    logger.debug(f"流式消息长度 {len(current_text)} 超过限制 {self._max_message_length}，启用新消息")
                    with self._lock:
                        self._msg_start_offset += len(self._sent_text)
                        current_text = self._buffer[self._msg_start_offset :]
                    self._message_response = None
                    self._sent_text = ""

                    # 如果偏移后还有新内容，立即发送为新消息
                    if current_text:
                        response = await run_in_threadpool(
                            chain.send_direct_message,
                            Message(
                                channel=self._channel,
                                source=self._source,
                                mtype=MessageType.Agent,
                                userid=self._user_id,
                                username=self._username,
                                original_message_id=self._original_message_id,
                                original_chat_id=self._original_chat_id,
                                title=self._title,
                                text=current_text,
                                rich_message=self._get_rich_message(current_text),
                                save_history=False,
                            ),
                        )
                        if response and response.success and response.message_id:
                            self._message_response = response
                            with self._lock:
                                self._sent_text = current_text
                            logger.debug(f"流式输出新消息已发送: message_id={response.message_id}")
                        else:
                            logger.debug("流式输出新消息发送失败，降级为非流式输出")
                            self._streaming_enabled = False
                else:
                    # 后续更新：编辑已有消息
                    try:
                        channel_enum = NotificationChannel(self._channel)
                    except (ValueError, KeyError):
                        return

                    metadata = dict(self._message_response.metadata or {})
                    rich_message = self._get_rich_message(current_text)
                    if rich_message:
                        # 通用编辑接口不增加渠道专属参数，通过元数据交给 Telegram 模块消费。
                        metadata["telegram_rich_message"] = rich_message
                    success = await run_in_threadpool(
                        chain.edit_message,
                        channel=channel_enum,
                        source=self._message_response.source,
                        message_id=self._message_response.message_id,
                        chat_id=self._message_response.chat_id,
                        text=current_text,
                        title=self._title,
                        metadata=metadata,
                    )
                    if success:
                        with self._lock:
                            self._sent_text = current_text
                    else:
                        logger.debug("流式输出消息编辑失败")
        except Exception as e:
            logger.error(f"流式输出刷新失败: {e}")

    @property
    def is_streaming(self) -> bool:
        """
        是否正在流式输出
        """
        return self._streaming_enabled

    @property
    def is_auto_flushing(self) -> bool:
        """
        是否正在定时刷新（渠道支持消息编辑时自动推送 buffer 内容）
        """
        return self._flush_task is not None

    @property
    def has_sent_message(self) -> bool:
        """
        是否已经通过流式输出发送过消息（当前轮次）
        """
        return self._message_response is not None

    @property
    def last_buffer_char(self) -> str:
        """
        返回当前缓冲区最后一个字符；缓冲区为空时返回空字符串。
        """
        with self._lock:
            return self._buffer[-1:] if self._buffer else ""
