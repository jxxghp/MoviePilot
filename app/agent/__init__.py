import asyncio
import json
import re
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
)
from langchain_core.messages import (  # noqa: F401
    HumanMessage,
    BaseMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.callback import StreamingHandler
from app.agent.llm import LLMHelper
from app.agent.memory import memory_manager
from app.agent.middleware.activity_log import ActivityLogMiddleware
from app.agent.middleware.jobs import (
    JobsMiddleware,
    filter_active_jobs,
    load_jobs_metadata,
)
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.patch_tool_calls import PatchToolCallsMiddleware
from app.agent.middleware.runtime_config import RuntimeConfigMiddleware
from app.agent.middleware.skills import SkillsMiddleware
from app.agent.middleware.tool_selection import ToolSelectorMiddleware
from app.agent.middleware.usage import UsageMiddleware
from app.agent.prompt import prompt_manager
from app.agent.runtime import agent_runtime_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.chain import ChainBase
from app.core.config import settings
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.message import ChannelCapabilityManager, ChannelCapability
from app.schemas.types import MessageChannel
from app.utils.identity import SYSTEM_INTERNAL_USER_ID


class AgentChain(ChainBase):
    pass


@dataclass
class _SessionUsageSnapshot:
    model: Optional[str] = None
    context_window_tokens: Optional[int] = None
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_total_tokens: int = 0
    last_context_usage_ratio: Optional[float] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    model_call_count: int = 0
    last_updated_at: Optional[datetime] = None

    def to_dict(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "model": self.model,
            "context_window_tokens": self.context_window_tokens,
            "last_input_tokens": self.last_input_tokens,
            "last_output_tokens": self.last_output_tokens,
            "last_total_tokens": self.last_total_tokens,
            "last_context_usage_ratio": self.last_context_usage_ratio,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "model_call_count": self.model_call_count,
            "last_updated_at": self.last_updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if self.last_updated_at
            else None,
        }


class _ThinkTagStripper:
    """
    流式剥离 <think>...</think> 标签的辅助类。
    维护内部缓冲区，处理标签跨 token 边界被截断的情况。
    """

    def __init__(self):
        self.buffer = ""
        self.in_think_tag = False

    def reset(self):
        """重置状态"""
        self.buffer = ""
        self.in_think_tag = False

    def process(self, text: str, on_output: Callable[[str], None]):
        """
        将新文本送入处理，剥离 <think> 标签后通过 on_output 回调输出。
        :param text: 新增的文本片段
        :param on_output: 输出回调，接收过滤后的文本
        :return: 本次调用是否通过 on_output 输出了内容
        """
        self.buffer += text
        emitted = False
        while self.buffer:
            if not self.in_think_tag:
                start_idx = self.buffer.find("<think>")
                if start_idx != -1:
                    if start_idx > 0:
                        on_output(self.buffer[:start_idx])
                        emitted = True
                    self.in_think_tag = True
                    self.buffer = self.buffer[start_idx + 7:]
                else:
                    # 检查是否以 <think> 的不完整前缀结尾
                    partial_match = False
                    for i in range(6, 0, -1):
                        if self.buffer.endswith("<think>"[:i]):
                            if len(self.buffer) > i:
                                on_output(self.buffer[:-i])
                                emitted = True
                            self.buffer = self.buffer[-i:]
                            partial_match = True
                            break
                    if not partial_match:
                        on_output(self.buffer)
                        emitted = True
                        self.buffer = ""
            else:
                end_idx = self.buffer.find("</think>")
                if end_idx != -1:
                    self.in_think_tag = False
                    self.buffer = self.buffer[end_idx + 8:]
                else:
                    # 检查是否以 </think> 的不完整前缀结尾
                    partial_match = False
                    for i in range(7, 0, -1):
                        if self.buffer.endswith("</think>"[:i]):
                            self.buffer = self.buffer[-i:]
                            partial_match = True
                            break
                    if not partial_match:
                        self.buffer = ""
                    break
        return emitted

    def flush(self, on_output: Callable[[str], None]):
        """流式结束时，输出缓冲区中剩余的非思考内容"""
        if self.buffer and not self.in_think_tag:
            on_output(self.buffer)
            self.buffer = ""


class ReplyMode(str, Enum):
    """
    Agent 最终回复处理模式。
    """

    DISPATCH = "dispatch"
    CAPTURE_ONLY = "capture_only"


HEARTBEAT_SESSION_PREFIX = "__agent_heartbeat_"


class MoviePilotAgent:
    """
    MoviePilot AI智能体（基于 LangChain v1 + LangGraph）
    """

    def __init__(
            self,
            session_id: str,
            user_id: str = None,
            channel: str = None,
            source: str = None,
            username: str = None,
            replay_mode: ReplyMode = ReplyMode.DISPATCH,
            persist_output_message: bool = True,
            allow_message_tools: bool = True,
            output_callback: Optional[Callable[[str], None]] = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.channel = channel
        self.source = source
        self.username = username
        self.reply_mode = replay_mode
        self.persist_output_message = persist_output_message
        self.allow_message_tools = allow_message_tools
        self.output_callback = output_callback
        self._tool_context: Dict[str, object] = {}
        self._streamed_output = ""
        self._session_usage = _SessionUsageSnapshot()

        # 流式token管理
        self.stream_handler = StreamingHandler()

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _get_model_name(cls, model: Any) -> Optional[str]:
        return (
                getattr(model, "model", None)
                or getattr(model, "model_name", None)
                or getattr(model, "model_id", None)
        )

    @classmethod
    def _get_context_window_tokens(cls, model: Any) -> Optional[int]:
        profile = getattr(model, "profile", None)
        if not profile:
            return None
        if isinstance(profile, dict):
            return cls._coerce_int(
                profile.get("max_input_tokens") or profile.get("input_token_limit")
            )
        return cls._coerce_int(
            getattr(profile, "max_input_tokens", None)
            or getattr(profile, "input_token_limit", None)
        )

    def _sync_model_profile(self, model: Any) -> None:
        model_name = self._get_model_name(model)
        context_window_tokens = self._get_context_window_tokens(model)
        if model_name:
            self._session_usage.model = model_name
        if context_window_tokens:
            self._session_usage.context_window_tokens = context_window_tokens

    def _record_usage(self, usage: dict[str, Any]) -> None:
        if not usage:
            return

        model_name = usage.get("model")
        context_window_tokens = self._coerce_int(usage.get("context_window_tokens"))
        if model_name:
            self._session_usage.model = model_name
        if context_window_tokens:
            self._session_usage.context_window_tokens = context_window_tokens

        self._session_usage.model_call_count += 1
        self._session_usage.last_updated_at = datetime.now()

        if not usage.get("has_usage"):
            return

        input_tokens = self._coerce_int(usage.get("input_tokens")) or 0
        output_tokens = self._coerce_int(usage.get("output_tokens")) or 0
        total_tokens = self._coerce_int(usage.get("total_tokens"))
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens

        self._session_usage.last_input_tokens = input_tokens
        self._session_usage.last_output_tokens = output_tokens
        self._session_usage.last_total_tokens = total_tokens
        self._session_usage.last_context_usage_ratio = usage.get("context_usage_ratio")
        self._session_usage.total_input_tokens += input_tokens
        self._session_usage.total_output_tokens += output_tokens
        self._session_usage.total_tokens += total_tokens

    def get_session_status(self) -> dict[str, Any]:
        if not self._session_usage.model:
            self._session_usage.model = settings.LLM_MODEL
        if not self._session_usage.context_window_tokens:
            self._session_usage.context_window_tokens = (
                settings.LLM_MAX_CONTEXT_TOKENS * 1000
                if settings.LLM_MAX_CONTEXT_TOKENS
                else None
            )
        return self._session_usage.to_dict(self.session_id)

    @property
    def is_background(self) -> bool:
        """
        是否为后台任务模式（无渠道信息，如定时唤醒）
        """
        return (not self.channel or not self.source) and not callable(self.output_callback)

    @property
    def should_dispatch_reply(self) -> bool:
        """
        是否应将最终回复真正发送到消息渠道。
        """
        return self.reply_mode == ReplyMode.DISPATCH

    @property
    def is_heartbeat_session(self) -> bool:
        """
        是否为后台心跳会话。

        心跳场景只负责检查并执行待处理 job，不需要携带近期活动日志，
        否则会让这类高频后台调用持续带入无关动态上下文，影响缓存命中率。
        """
        return self.session_id.startswith(HEARTBEAT_SESSION_PREFIX)

    def _should_stream(self) -> bool:
        """
        判断是否应启用流式输出：
        - 后台模式不启用流式输出
        - 渠道支持消息编辑：启用流式输出（实时推送 token）
        - 渠道不支持消息编辑但开启了啰嗦模式：也需要启用流式输出，
          以便在工具调用前捕获 Agent 的中间文字并随工具消息一起发送
        - 其他情况不启用流式输出
        """
        if self.is_background:
            return False
        # 啰嗦模式下始终需要流式输出来捕获工具调用前的 Agent 文字
        if settings.AI_AGENT_VERBOSE:
            return True
        try:
            channel_enum = MessageChannel(self.channel)
            return ChannelCapabilityManager.supports_capability(
                channel_enum, ChannelCapability.MESSAGE_EDITING
            )
        except (ValueError, KeyError):
            return False

    @staticmethod
    async def _initialize_llm(streaming: bool = False):
        """
        初始化 LLM
        :param streaming: 是否启用流式输出
        """
        return await LLMHelper.get_llm(streaming=streaming)

    @staticmethod
    def _extract_text_content(content) -> str:
        """
        从消息内容中提取纯文本，过滤掉思考/推理类型的内容块。
        :param content: 消息内容，可能是字符串或内容块列表
        :return: 纯文本内容
        """
        if not content:
            return ""
        # 跳过思考/推理类型的内容块
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    # 优先检查 thought 标志（LangChain Google GenAI 方案）
                    if block.get("thought"):
                        continue
                    if block.get("type") in (
                            "thinking",
                            "reasoning_content",
                            "reasoning",
                            "thought",
                    ):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    else:
                        text_parts.append(str(block))
            return "".join(text_parts)
        return str(content)

    def _emit_output(self, text: str):
        """
        输出当前流式文本到外部回调。
        """
        if not text:
            return
        self._streamed_output += text
        if not callable(self.output_callback):
            return
        try:
            self.output_callback(self._streamed_output)
        except Exception as e:
            logger.debug(f"智能体输出回调失败: {e}")

    def _handle_stream_text(self, text: str):
        """
        统一处理一段可见流式文本，确保工具统计注入后的内容会同时进入
        消息缓冲区和外部流式回调。
        """
        emitted_text = self.stream_handler.emit(text)
        self._emit_output(emitted_text)

    def _initialize_tools(self) -> List:
        """
        初始化工具列表
        """
        return MoviePilotToolFactory.create_tools(
            session_id=self.session_id,
            user_id=self.user_id,
            channel=self.channel,
            source=self.source,
            username=self.username,
            stream_handler=self.stream_handler,
            agent_context=self._tool_context,
            allow_message_tools=self.allow_message_tools,
        )

    async def _create_agent(self, streaming: bool = False):
        """
        创建 LangGraph Agent（使用 create_agent + SummarizationMiddleware）
        :param streaming: 是否启用流式输出
        """
        try:
            # 系统提示词
            system_prompt = prompt_manager.get_agent_prompt(channel=self.channel)

            # LLM 模型（用于 agent 执行）
            agent_model = await self._initialize_llm(streaming=streaming)
            self._sync_model_profile(agent_model)

            # 为内部模型调用准备非流式 LLM，避免与用户流式回复复用同一实例。
            non_streaming_model = (
                agent_model
                if not streaming
                else await self._initialize_llm(streaming=False)
            )

            # 工具列表
            tools = self._initialize_tools()
            max_tools = settings.LLM_MAX_TOOLS
            always_include_tools = (
                MoviePilotToolFactory.get_tool_selector_always_include_names(tools)
            )

            # 中间件
            middlewares = [
                # Skills
                SkillsMiddleware(
                    sources=[str(agent_runtime_manager.skills_dir)],
                    bundled_skills_dir=str(settings.ROOT_PATH / "skills"),
                ),
                # Jobs 任务管理
                JobsMiddleware(
                    sources=[str(agent_runtime_manager.jobs_dir)],
                ),
                # 运行时人格与核心规则
                RuntimeConfigMiddleware(),
                # 记忆管理
                MemoryMiddleware(memory_dir=str(agent_runtime_manager.memory_dir)),
                # 上下文压缩
                SummarizationMiddleware(
                    model=non_streaming_model, trigger=("fraction", 0.85)
                ),
                # 错误工具调用修复
                PatchToolCallsMiddleware(),
                # 用量统计
                UsageMiddleware(on_usage=self._record_usage),
            ]

            if not self.is_heartbeat_session:
                middlewares.insert(
                    4,
                    ActivityLogMiddleware(
                        activity_dir=str(agent_runtime_manager.activity_dir),
                    ),
                )

            # 工具选择
            if max_tools > 0:
                middlewares.append(
                    ToolSelectorMiddleware(
                        model=non_streaming_model,
                        selection_tools=tools,
                        max_tools=max_tools,
                        always_include=always_include_tools,
                    )
                )

            return create_agent(
                model=agent_model,
                tools=tools,
                system_prompt=system_prompt,
                middleware=middlewares,
                checkpointer=InMemorySaver(),
            )
        except Exception as e:
            logger.error(f"创建 Agent 失败: {e}")
            raise e

    async def process(
            self,
            message: str,
            images: List[str] = None,
            files: Optional[List[dict]] = None,
    ) -> str:
        """
        处理用户消息，流式推理并返回 Agent 回复
        """
        try:
            logger.info(
                f"Agent推理: session_id={self.session_id}, input={message}, "
                f"images={len(images) if images else 0}, files={len(files) if files else 0}"
            )
            self._tool_context = {
                "user_reply_sent": False,
                "reply_mode": None,
                "should_dispatch_reply": self.should_dispatch_reply,
            }
            self._streamed_output = ""

            # 获取历史消息
            messages = memory_manager.get_agent_messages(
                session_id=self.session_id, user_id=self.user_id
            )

            # 构建结构化用户消息内容
            request_payload = {
                "message": message or "",
                "images": [
                    {"index": index + 1, "type": "image"}
                    for index, _ in enumerate(images or [])
                ],
                "files": files or [],
            }
            content = [
                {
                    "type": "text",
                    "text": json.dumps(request_payload, ensure_ascii=False, indent=2),
                }
            ]
            for img in images or []:
                content.append({"type": "image_url", "image_url": {"url": img}})
            messages.append(HumanMessage(content=content))

            # 执行推理
            await self._execute_agent(messages)

        except Exception as e:
            error_message = f"处理消息时发生错误: {str(e)}"
            logger.error(error_message)
            if not self.should_dispatch_reply:
                raise
            await self.send_agent_message(error_message)
            return error_message

    async def _stream_agent_tokens(
            self, agent, messages: dict, config: dict, on_token: Callable[[str], None]
    ):
        """
        流式运行智能体，过滤工具调用token和思考内容，将模型生成的内容通过回调输出。
        :param agent: LangGraph Agent 实例
        :param messages: Agent 输入消息
        :param config: Agent 运行配置
        :param on_token: 收到有效 token 时的回调
        """
        stripper = _ThinkTagStripper()

        async for chunk in agent.astream(
                messages,
                stream_mode="messages",
                config=config,
                subgraphs=False,
                version="v2",
        ):
            if chunk["type"] == "messages":
                token, metadata = chunk["data"]
                if not token or not hasattr(token, "tool_call_chunks"):
                    continue

                if token.tool_call_chunks:
                    # 清除 stripper 内部缓冲中可能残留的 <think> 标签中间状态
                    stripper.reset()
                    continue

                # 以下处理纯文本token（tool_call_chunks为空）

                # 跳过模型思考/推理内容（如 DeepSeek R1 的 reasoning_content）
                additional = getattr(token, "additional_kwargs", None)
                if additional and additional.get("reasoning_content"):
                    continue

                if token.content:
                    # content 可能是字符串或内容块列表，过滤掉思考类型的块
                    content = self._extract_text_content(token.content)
                    if content:
                        stripper.process(content, on_token)

        stripper.flush(on_token)

    async def _execute_agent(self, messages: List[BaseMessage]):
        """
        调用 LangGraph Agent 执行推理。
        根据运行环境选择不同的执行模式：
        - 后台任务模式（无渠道信息）：非流式 LLM + ainvoke，由 reply_mode 决定是发送还是仅捕获
        - 渠道不支持消息编辑：非流式 LLM + ainvoke，完成后发送最终回复
        - 渠道支持消息编辑：流式 LLM + astream，实时推送 token
        """
        try:
            # Agent运行配置
            agent_config = {
                "configurable": {
                    "thread_id": self.session_id,
                }
            }

            # 判断是否启用流式输出
            use_streaming = self._should_stream()

            # 创建智能体（根据是否流式传入不同 LLM）
            agent = await self._create_agent(streaming=use_streaming)

            if use_streaming:
                self.stream_handler.set_dispatch_policy(
                    allow_dispatch_without_context=self.should_dispatch_reply
                )
                # 流式模式：渠道支持消息编辑，启动流式输出实时推送 token
                await self.stream_handler.start_streaming(
                    channel=self.channel,
                    source=self.source,
                    user_id=self.user_id,
                    username=self.username,
                )

                # 流式运行智能体，token 直接推送到 stream_handler
                await self._stream_agent_tokens(
                    agent=agent,
                    messages={"messages": messages},
                    config=agent_config,
                    on_token=self._handle_stream_text,
                )

                # 输出流式过程中可能残留的工具调用统计信息
                trailing_tool_summary = self.stream_handler.flush_pending_tool_summary()
                if trailing_tool_summary:
                    self._emit_output(trailing_tool_summary)

                # 停止流式输出，返回是否已通过流式编辑发送了所有内容及最终文本
                (
                    all_sent_via_stream,
                    streamed_text,
                ) = await self.stream_handler.stop_streaming()

                if not all_sent_via_stream:
                    # 流式输出未能发送全部内容（发送失败等）
                    # 通过常规方式发送剩余内容
                    remaining_text = await self.stream_handler.take()
                    if remaining_text:
                        unsent_text = remaining_text
                        if self._streamed_output and remaining_text.startswith(
                                self._streamed_output
                        ):
                            unsent_text = remaining_text[len(self._streamed_output):]
                        if unsent_text:
                            self._emit_output(unsent_text)
                    if (
                            remaining_text
                            and self.should_dispatch_reply
                            and not self._tool_context.get("user_reply_sent")
                    ):
                        await self.send_agent_message(remaining_text)
                    elif (
                            remaining_text
                            and self.persist_output_message
                            and not self._tool_context.get("user_reply_sent")
                    ):
                        title = "MoviePilot助手" if self.is_background else ""
                        await self._save_agent_message_to_db(
                            remaining_text,
                            title=title,
                        )
                elif streamed_text and self.persist_output_message:
                    # 流式输出已发送全部内容，但未记录到数据库，补充保存消息记录
                    await self._save_agent_message_to_db(streamed_text)

            else:
                # 非流式模式：后台任务或渠道不支持消息编辑
                await agent.ainvoke(
                    {"messages": messages},
                    config=agent_config,
                )

                # 从最终状态中提取最后一条AI回复内容
                final_messages = agent.get_state(agent_config).values.get(
                    "messages", []
                )
                final_text = ""
                for msg in reversed(final_messages):
                    if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                        # 过滤掉思考/推理内容，只提取纯文本
                        text = self._extract_text_content(msg.content)
                        if text:
                            # 过滤掉包含在 <think> 标签中的内容
                            text = re.sub(
                                r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL
                            )
                            final_text = text.strip()
                            break

                if final_text and not self._streamed_output:
                    self._emit_output(final_text)

                if (
                        final_text
                        and self.should_dispatch_reply
                        and not self._tool_context.get("user_reply_sent")
                ):
                    if self.is_background:
                        # 后台任务发送最终回复时统一带标题
                        await self.send_agent_message(
                            final_text, title="MoviePilot助手"
                        )
                    else:
                        # 非流式渠道：发送最终回复
                        await self.send_agent_message(final_text)
                elif (
                        final_text
                        and self.persist_output_message
                        and not self._tool_context.get("user_reply_sent")
                ):
                    title = "MoviePilot助手" if self.is_background else ""
                    await self._save_agent_message_to_db(final_text, title=title)

            # 保存消息
            memory_manager.save_agent_messages(
                session_id=self.session_id,
                user_id=self.user_id,
                messages=agent.get_state(agent_config).values.get("messages", []),
            )

        except asyncio.CancelledError:
            logger.info(f"Agent执行被取消: session_id={self.session_id}")
            return "任务已取消", {}
        except Exception as e:
            logger.error(f"Agent执行失败: {e} - {traceback.format_exc()}")
            return str(e), {}
        finally:
            # 确保停止流式输出
            await self.stream_handler.stop_streaming()

    async def send_agent_message(self, message: str, title: str = ""):
        """
        通过原渠道发送消息给用户
        """
        await AgentChain().async_post_message(
            Notification(
                channel=self.channel,
                source=self.source,
                mtype=NotificationType.Agent,
                userid=self.user_id,
                username=self.username,
                title=title,
                text=message,
            )
        )

    async def _save_agent_message_to_db(self, message: str, title: str = ""):
        """
        仅保存Agent回复消息到数据库和SSE队列（不重新发送到渠道）
        用于流式输出场景：消息已通过 send_direct_message/edit_message 发送给用户，
        但未记录到数据库中，此方法补充保存消息历史记录。
        """
        chain = AgentChain()
        notification = Notification(
            channel=self.channel,
            source=self.source,
            userid=self.user_id,
            username=self.username,
            title=title,
            text=message,
        )
        # 保存到SSE消息队列（供前端展示）
        chain.messagehelper.put(notification, role="user", title=title)
        # 保存到数据库
        await chain.messageoper.async_add(**notification.model_dump())

    async def cleanup(self):
        """
        清理智能体资源
        """
        logger.info(f"MoviePilot智能体已清理: session_id={self.session_id}")


@dataclass
class _MessageTask:
    """
    待处理的消息任务
    """

    session_id: str
    user_id: str
    message: str
    images: Optional[List[str]] = None
    files: Optional[List[dict]] = None
    channel: Optional[str] = None
    source: Optional[str] = None
    username: Optional[str] = None
    reply_mode: ReplyMode = ReplyMode.DISPATCH


class AgentManager:
    """
    AI智能体管理器
    同一会话的消息按顺序排队处理，不同会话之间互不影响。
    """

    def __init__(self):
        self.active_agents: Dict[str, MoviePilotAgent] = {}
        # 每个会话的消息队列
        self._session_queues: Dict[str, asyncio.Queue] = {}
        # 每个会话的worker任务
        self._session_workers: Dict[str, asyncio.Task] = {}

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """获取会话当前模型与 token 使用状态。"""
        agent = self.active_agents.get(session_id)
        if agent:
            status = agent.get_session_status()
        else:
            status = {
                "session_id": session_id,
                "model": settings.LLM_MODEL,
                "context_window_tokens": settings.LLM_MAX_CONTEXT_TOKENS * 1000
                if settings.LLM_MAX_CONTEXT_TOKENS
                else None,
                "last_input_tokens": 0,
                "last_output_tokens": 0,
                "last_total_tokens": 0,
                "last_context_usage_ratio": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "model_call_count": 0,
                "last_updated_at": None,
            }

        queue = self._session_queues.get(session_id)
        status["pending_messages"] = queue.qsize() if queue else 0
        status["is_processing"] = (
                session_id in self._session_workers
                and not self._session_workers[session_id].done()
        )
        return status

    @staticmethod
    async def initialize():
        """
        初始化管理器
        """
        memory_manager.initialize()

    async def close(self):
        """
        关闭管理器
        """
        await memory_manager.close()
        # 取消所有会话worker
        for task in self._session_workers.values():
            task.cancel()
        # 等待所有worker结束
        for session_id, task in self._session_workers.items():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._session_workers.clear()
        self._session_queues.clear()
        for agent in self.active_agents.values():
            await agent.cleanup()
        self.active_agents.clear()

    async def process_message(
            self,
            session_id: str,
            user_id: str,
            message: str,
            images: List[str] = None,
            files: Optional[List[dict]] = None,
            channel: str = None,
            source: str = None,
            username: str = None,
            reply_mode: ReplyMode = ReplyMode.DISPATCH,
    ) -> str:
        """
        处理用户消息：将消息放入会话队列，按顺序依次处理。
        同一会话的消息排队等待，不同会话之间互不影响。
        """
        task = _MessageTask(
            session_id=session_id,
            user_id=user_id,
            message=message,
            images=images,
            files=files,
            channel=channel,
            source=source,
            username=username,
            reply_mode=reply_mode,
        )

        # 获取或创建会话队列
        if session_id not in self._session_queues:
            self._session_queues[session_id] = asyncio.Queue()

        queue = self._session_queues[session_id]
        queue_size = queue.qsize()

        # 如果队列中已有等待的消息，通知用户消息已排队
        if queue_size > 0 or (
                session_id in self._session_workers
                and not self._session_workers[session_id].done()
        ):
            logger.info(
                f"会话 {session_id} 有任务正在处理，消息已排队等待 "
                f"(队列中待处理: {queue_size} 条)"
            )

        # 放入队列
        await queue.put(task)

        # 确保该会话有一个worker在运行
        if (
                session_id not in self._session_workers
                or self._session_workers[session_id].done()
        ):
            self._session_workers[session_id] = asyncio.create_task(
                self._session_worker(session_id)
            )

        return ""

    async def _session_worker(self, session_id: str):
        """
        会话消息处理worker：从队列中逐条取出消息并处理。
        处理完当前消息后才会处理下一条，确保同一会话的消息顺序执行。
        """
        queue = self._session_queues.get(session_id)
        if not queue:
            return

        try:
            while True:
                try:
                    # 等待消息，超时后自动退出worker
                    task = await asyncio.wait_for(queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    # 队列空闲超时，退出worker
                    logger.debug(f"会话 {session_id} 的消息队列空闲，worker退出")
                    break

                try:
                    await self._process_message_internal(task)
                except Exception as e:
                    logger.error(f"处理会话 {session_id} 的消息失败: {e}")
                finally:
                    queue.task_done()

        except asyncio.CancelledError:
            logger.info(f"会话 {session_id} 的worker被取消")
        finally:
            # 清理已完成的worker记录
            self._session_workers.pop(session_id, None)  # noqa
            # 如果队列为空，清理队列
            if (
                    session_id in self._session_queues
                    and self._session_queues[session_id].empty()
            ):
                self._session_queues.pop(session_id, None)

    async def _process_message_internal(self, task: _MessageTask):
        """
        实际处理单条消息
        """
        session_id = task.session_id
        if session_id not in self.active_agents:
            logger.info(
                f"创建新的AI智能体实例，session_id: {session_id}, user_id: {task.user_id}"
            )
            agent = MoviePilotAgent(
                session_id=session_id,
                user_id=task.user_id,
                channel=task.channel,
                source=task.source,
                username=task.username,
                replay_mode=task.reply_mode,
            )
            self.active_agents[session_id] = agent
        else:
            agent = self.active_agents[session_id]
            agent.user_id = task.user_id
            if task.channel:
                agent.channel = task.channel
            if task.source:
                agent.source = task.source
            if task.username:
                agent.username = task.username
            agent.reply_mode = task.reply_mode

        return await agent.process(task.message, images=task.images, files=task.files)

    async def stop_current_task(self, session_id: str):
        """
        应急停止当前正在执行的Agent推理任务，但保留会话和记忆。
        与 clear_session 不同，此方法不会销毁Agent实例或清除记忆，
        用户可以在停止后继续对话。
        """
        stopped = False

        # 取消该会话的worker（会触发 _execute_agent 中的 CancelledError）
        if session_id in self._session_workers:
            self._session_workers[session_id].cancel()
            try:
                await self._session_workers[session_id]
            except asyncio.CancelledError:
                pass
            self._session_workers.pop(session_id, None)  # noqa
            stopped = True

        # 清空队列中待处理的消息
        if session_id in self._session_queues:
            queue = self._session_queues[session_id]
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._session_queues.pop(session_id, None)
            stopped = True

        if stopped:
            logger.info(f"会话 {session_id} 的Agent推理已应急停止")
        else:
            logger.debug(f"会话 {session_id} 没有正在执行的Agent任务")

        return stopped

    async def clear_session(self, session_id: str, user_id: str):
        """
        清空会话
        """
        # 取消该会话的worker
        if session_id in self._session_workers:
            self._session_workers[session_id].cancel()
            try:
                await self._session_workers[session_id]
            except asyncio.CancelledError:
                pass
            await self._session_workers.pop(session_id, None)

        # 清理队列
        self._session_queues.pop(session_id, None)

        # 清理agent
        if session_id in self.active_agents:
            agent = self.active_agents[session_id]
            await agent.cleanup()
            del self.active_agents[session_id]
            memory_manager.clear_memory(session_id, user_id)
            logger.info(f"会话 {session_id} 的记忆已清空")

    @staticmethod
    async def run_background_prompt(
            message: str,
            session_prefix: str = "__agent_background",
            output_callback: Optional[Callable[[str], None]] = None,
            reply_mode: ReplyMode = ReplyMode.CAPTURE_ONLY,
            persist_output_message: bool = True,
            allow_message_tools: Optional[bool] = None,
    ) -> None:
        """
        以独立后台会话执行一段 prompt。
        """
        session_id = f"{session_prefix}_{uuid.uuid4().hex[:8]}__"
        user_id = SYSTEM_INTERNAL_USER_ID

        if reply_mode == ReplyMode.CAPTURE_ONLY:
            allow_message_tools = False
        elif allow_message_tools is None:
            allow_message_tools = True

        agent = MoviePilotAgent(
            session_id=session_id,
            user_id=user_id,
            channel=None,
            source=None,
            username=settings.SUPERUSER,
            replay_mode=reply_mode,
            persist_output_message=persist_output_message,
            output_callback=output_callback,
            allow_message_tools=allow_message_tools,
        )

        try:
            await agent.process(message)
        finally:
            await agent.cleanup()
            memory_manager.clear_memory(session_id, user_id)

    @staticmethod
    def _build_heartbeat_prompt() -> str:
        """使用程序内置 System Tasks 定义构建心跳任务提示词。"""
        return prompt_manager.render_system_task_message("heartbeat")

    async def heartbeat_check_jobs(self):
        """
        心跳唤醒：检查并执行待处理的定时任务（Jobs）。
        由定时调度器周期性调用，每次使用独立的会话避免上下文干扰。
        """
        try:
            active_jobs = filter_active_jobs(
                await load_jobs_metadata([str(agent_runtime_manager.jobs_dir)])
            )
            # 先在本地判断是否存在活跃任务。没有任务时直接短路，避免一次完整
            # 的后台 Agent/LLM 空调用。
            if not active_jobs:
                logger.info("智能体心跳唤醒：没有活跃任务，跳过模型调用")
                return

            # 每次使用唯一的 session_id，避免共享上下文
            session_id = f"{HEARTBEAT_SESSION_PREFIX}{uuid.uuid4().hex[:12]}__"
            user_id = SYSTEM_INTERNAL_USER_ID

            logger.info("智能体心跳唤醒：开始检查待处理任务...")
            heartbeat_message = self._build_heartbeat_prompt()

            await self.process_message(
                session_id=session_id,
                user_id=user_id,
                message=heartbeat_message,
                channel=None,
                source=None,
                username=settings.SUPERUSER,
                reply_mode=ReplyMode.DISPATCH,
            )

            # 等待消息队列处理完成
            if session_id in self._session_queues:
                await self._session_queues[session_id].join()

            # 等待worker结束
            if session_id in self._session_workers:
                try:
                    await self._session_workers[session_id]
                except asyncio.CancelledError:
                    pass

            logger.info("智能体心跳唤醒：任务检查完成")

            # 心跳会话用完即弃，清理资源
            await self.clear_session(session_id, user_id)

        except Exception as e:
            logger.error(f"智能体心跳唤醒失败: {e}")


# 全局智能体管理器实例
agent_manager = AgentManager()
