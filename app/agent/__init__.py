import asyncio
import re
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
    LLMToolSelectorMiddleware,
)
from langchain_core.messages import (  # noqa: F401
    HumanMessage,
    BaseMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.callback import StreamingHandler
from app.agent.memory import memory_manager
from app.agent.middleware.activity_log import ActivityLogMiddleware
from app.agent.middleware.jobs import JobsMiddleware
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.patch_tool_calls import PatchToolCallsMiddleware
from app.agent.middleware.skills import SkillsMiddleware
from app.agent.prompt import prompt_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.chain import ChainBase
from app.core.config import settings
from app.helper.llm import LLMHelper
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.message import ChannelCapabilityManager, ChannelCapability
from app.schemas.types import MessageChannel


class AgentChain(ChainBase):
    pass


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
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.channel = channel
        self.source = source
        self.username = username
        self.reply_with_voice = False
        self._tool_context: Dict[str, object] = {}

        # 流式token管理
        self.stream_handler = StreamingHandler()

    @property
    def is_background(self) -> bool:
        """
        是否为后台任务模式（无渠道信息，如定时唤醒）
        """
        return not self.channel or not self.source

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
        if self.reply_with_voice:
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
    def _initialize_llm(streaming: bool = False):
        """
        初始化 LLM
        :param streaming: 是否启用流式输出
        """
        return LLMHelper.get_llm(streaming=streaming)

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
        )

    def _create_agent(self, streaming: bool = False):
        """
        创建 LangGraph Agent（使用 create_agent + SummarizationMiddleware）
        :param streaming: 是否启用流式输出
        """
        try:
            # 系统提示词
            system_prompt = prompt_manager.get_agent_prompt(
                channel=self.channel,
                prefer_voice_reply=self.reply_with_voice,
            )

            # LLM 模型（用于 agent 执行）
            llm = self._initialize_llm(streaming=streaming)

            # 工具列表
            tools = self._initialize_tools()

            # 中间件
            middlewares = [
                # Skills
                SkillsMiddleware(
                    sources=[str(settings.CONFIG_PATH / "agent" / "skills")],
                    bundled_skills_dir=str(settings.ROOT_PATH / "skills"),
                ),
                # Jobs 任务管理
                JobsMiddleware(
                    sources=[str(settings.CONFIG_PATH / "agent" / "jobs")],
                ),
                # 记忆管理（自动扫描 agent 目录下所有 .md 文件）
                MemoryMiddleware(memory_dir=str(settings.CONFIG_PATH / "agent")),
                # 活动日志
                ActivityLogMiddleware(
                    activity_dir=str(settings.CONFIG_PATH / "agent" / "activity"),
                ),
                # 上下文压缩
                SummarizationMiddleware(model=llm, trigger=("fraction", 0.85)),
                # 错误工具调用修复
                PatchToolCallsMiddleware(),
            ]

            # 工具选择
            if settings.LLM_MAX_TOOLS > 0:
                middlewares.append(
                    LLMToolSelectorMiddleware(
                        model=llm, max_tools=settings.LLM_MAX_TOOLS
                    )
                )

            return create_agent(
                model=llm,
                tools=tools,
                system_prompt=system_prompt,
                middleware=middlewares,
                checkpointer=InMemorySaver(),
            )
        except Exception as e:
            logger.error(f"创建 Agent 失败: {e}")
            raise e

    async def process(self, message: str, images: List[str] = None) -> str:
        """
        处理用户消息，流式推理并返回 Agent 回复
        """
        try:
            logger.info(
                f"Agent推理: session_id={self.session_id}, input={message}, images={len(images) if images else 0}"
            )
            self._tool_context = {
                "incoming_voice": self.reply_with_voice,
                "user_reply_sent": False,
                "reply_mode": None,
            }

            # 获取历史消息
            messages = memory_manager.get_agent_messages(
                session_id=self.session_id, user_id=self.user_id
            )

            # 构建用户消息内容
            if images:
                content = []
                if message:
                    content.append({"type": "text", "text": message})
                for img in images:
                    content.append({"type": "image_url", "image_url": {"url": img}})
                messages.append(HumanMessage(content=content))
            else:
                messages.append(HumanMessage(content=message))

            # 执行推理
            await self._execute_agent(messages)

        except Exception as e:
            error_message = f"处理消息时发生错误: {str(e)}"
            logger.error(error_message)
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
        # 非VERBOSE模式下，跟踪当前langgraph_step以检测中间步骤的模型输出
        # 当模型在工具调用之前输出的"计划/思考"文本，会在检测到tool_call时被清除
        current_model_step = -1
        has_emitted_in_step = False

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

                # 获取当前步骤信息
                step = metadata.get("langgraph_step", -1) if metadata else -1

                if token.tool_call_chunks:
                    # 检测到工具调用token：说明当前步骤是中间步骤
                    # 非VERBOSE模式下，清除该步骤之前输出的"计划/思考"文本
                    if not settings.AI_AGENT_VERBOSE and has_emitted_in_step:
                        self.stream_handler.reset()
                        stripper.reset()
                        has_emitted_in_step = False
                    continue

                # 以下处理纯文本token（tool_call_chunks为空）

                # 检测步骤变化，重置步骤内emit跟踪
                if step != current_model_step:
                    current_model_step = step
                    has_emitted_in_step = False

                # 跳过模型思考/推理内容（如 DeepSeek R1 的 reasoning_content）
                additional = getattr(token, "additional_kwargs", None)
                if additional and additional.get("reasoning_content"):
                    continue

                if token.content:
                    # content 可能是字符串或内容块列表，过滤掉思考类型的块
                    content = self._extract_text_content(token.content)
                    if content:
                        if stripper.process(content, on_token):
                            has_emitted_in_step = True

        stripper.flush(on_token)

    async def _execute_agent(self, messages: List[BaseMessage]):
        """
        调用 LangGraph Agent 执行推理。
        根据运行环境选择不同的执行模式：
        - 后台任务模式（无渠道信息）：非流式 LLM + ainvoke，仅广播最终结果
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
            agent = self._create_agent(streaming=use_streaming)

            if use_streaming:
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
                    on_token=self.stream_handler.emit,
                )

                # 停止流式输出，返回是否已通过流式编辑发送了所有内容及最终文本
                (
                    all_sent_via_stream,
                    streamed_text,
                ) = await self.stream_handler.stop_streaming()

                if not all_sent_via_stream:
                    # 流式输出未能发送全部内容（发送失败等）
                    # 通过常规方式发送剩余内容
                    remaining_text = await self.stream_handler.take()
                    if remaining_text and not self._tool_context.get("user_reply_sent"):
                        await self.send_agent_message(remaining_text)
                elif streamed_text:
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

                if final_text and not self._tool_context.get("user_reply_sent"):
                    if self.is_background:
                        # 后台任务仅广播最终回复，带标题
                        await self.send_agent_message(
                            final_text, title="MoviePilot助手"
                        )
                    else:
                        # 非流式渠道：发送最终回复
                        await self.send_agent_message(final_text)

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
        user_id = self.user_id
        if self.user_id == "system":
            user_id = None

        await AgentChain().async_post_message(
            Notification(
                channel=self.channel,
                source=self.source,
                mtype=NotificationType.Agent,
                userid=user_id,
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
    channel: Optional[str] = None
    source: Optional[str] = None
    username: Optional[str] = None
    reply_with_voice: bool = False


class AgentManager:
    """
    AI智能体管理器
    同一会话的消息按顺序排队处理，不同会话之间互不影响。
    """

    # 批量重试整理的等待时间（秒），同一批次内的失败记录会合并为一次agent调用
    RETRY_TRANSFER_DEBOUNCE_SECONDS = 300

    def __init__(self):
        self.active_agents: Dict[str, MoviePilotAgent] = {}
        # 每个会话的消息队列
        self._session_queues: Dict[str, asyncio.Queue] = {}
        # 每个会话的worker任务
        self._session_workers: Dict[str, asyncio.Task] = {}
        # 重试整理的 debounce 缓冲区: group_key -> List[history_id]
        self._retry_transfer_buffer: Dict[str, List[int]] = {}
        # 重试整理的 debounce 定时器: group_key -> asyncio.TimerHandle
        self._retry_transfer_timers: Dict[str, asyncio.TimerHandle] = {}
        # 重试整理缓冲区锁
        self._retry_transfer_lock = asyncio.Lock()

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
        # 取消所有重试整理的延迟定时器
        for timer in self._retry_transfer_timers.values():
            timer.cancel()
        self._retry_transfer_timers.clear()
        self._retry_transfer_buffer.clear()
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
        channel: str = None,
        source: str = None,
        username: str = None,
        reply_with_voice: bool = False,
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
            channel=channel,
            source=source,
            username=username,
            reply_with_voice=reply_with_voice,
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
        agent.reply_with_voice = task.reply_with_voice

        return await agent.process(task.message, images=task.images)

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

    async def heartbeat_check_jobs(self):
        """
        心跳唤醒：检查并执行待处理的定时任务（Jobs）。
        由定时调度器周期性调用，每次使用独立的会话避免上下文干扰。
        """
        try:
            # 每次使用唯一的 session_id，避免共享上下文
            session_id = f"__agent_heartbeat_{uuid.uuid4().hex[:12]}__"
            user_id = "system"

            logger.info("智能体心跳唤醒：开始检查待处理任务...")

            # 英文提示词，便于大模型理解
            heartbeat_message = (
                "[System Heartbeat] Check all jobs in your jobs directory and process pending tasks:\n"
                "1. List all jobs with status 'pending' or 'in_progress'\n"
                "2. For 'recurring' jobs, check 'last_run' to determine if it's time to run again\n"
                "3. For 'once' jobs with status 'pending', execute them now\n"
                "4. After executing each job, update its status, 'last_run' time, and execution log in the JOB.md file\n"
                "5. If there are no pending jobs, do NOT generate any response\n\n"
                "IMPORTANT: This is a background system task, NOT a user conversation. "
                "Your final response will be broadcast as a notification. "
                "Only output a brief completion summary listing each executed job and its result. "
                "Do NOT include greetings, explanations, or conversational text. "
                "If no jobs were executed, output nothing. "
                "Respond in Chinese (中文)."
            )

            await self.process_message(
                session_id=session_id,
                user_id=user_id,
                message=heartbeat_message,
                channel=None,
                source=None,
                username=settings.SUPERUSER,
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

    async def retry_failed_transfer(self, history_id: int, group_key: str = ""):
        """
        触发智能体重新整理失败的历史记录。
        由文件整理模块在检测到整理失败后调用。
        同一 group_key 的失败记录会在缓冲期内合并为一次agent调用，避免重复浪费token。
        :param history_id: 失败的整理历史记录ID
        :param group_key: 分组键，相同key的记录会被合并处理（如download_hash、源目录等）
        """
        if not group_key:
            group_key = f"_default_{history_id}"

        async with self._retry_transfer_lock:
            # 将 history_id 加入缓冲区
            if group_key not in self._retry_transfer_buffer:
                self._retry_transfer_buffer[group_key] = []
            if history_id not in self._retry_transfer_buffer[group_key]:
                self._retry_transfer_buffer[group_key].append(history_id)
                logger.info(
                    f"智能体重试整理：记录 ID={history_id} 已加入缓冲区 "
                    f"(group={group_key}, 当前{len(self._retry_transfer_buffer[group_key])}条)"
                )

            # 取消该分组的旧定时器
            if group_key in self._retry_transfer_timers:
                self._retry_transfer_timers[group_key].cancel()

            # 设置新的延迟定时器
            loop = asyncio.get_running_loop()
            self._retry_transfer_timers[group_key] = loop.call_later(
                self.RETRY_TRANSFER_DEBOUNCE_SECONDS,
                lambda gk=group_key: asyncio.ensure_future(
                    self._flush_retry_transfer(gk)
                ),
            )

    async def _flush_retry_transfer(self, group_key: str):
        """
        延迟定时器到期后，取出该分组的所有 history_id 并合并为一次agent调用。
        """
        async with self._retry_transfer_lock:
            history_ids = self._retry_transfer_buffer.pop(group_key, [])
            self._retry_transfer_timers.pop(group_key, None)

        if not history_ids:
            return

        session_id = f"__agent_retry_transfer_batch_{uuid.uuid4().hex[:8]}__"
        user_id = "system"

        ids_str = ", ".join(str(i) for i in history_ids)
        logger.info(
            f"智能体重试整理：开始批量处理失败记录 IDs=[{ids_str}] (group={group_key})"
        )

        if len(history_ids) == 1:
            # 单条记录，使用原有逻辑
            retry_message = (
                f"[System Task - Transfer Failed Retry] A file transfer/organization has failed. "
                f"Please use the 'transfer-failed-retry' skill to retry the failed transfer.\n\n"
                f"Failed transfer history record ID: {history_ids[0]}\n\n"
                f"Follow these steps:\n"
                f"1. Use `query_transfer_history` with status='failed' to find the record with id={history_ids[0]} "
                f"and understand the failure details (source path, error message, media info)\n"
                f"2. Analyze the error message to determine the best retry strategy\n"
                f"3. If the source file no longer exists, skip this retry and report that the file is missing\n"
                f"4. Delete the failed history record using `delete_transfer_history` with history_id={history_ids[0]}\n"
                f"5. Re-identify the media using `recognize_media` with the source file path\n"
                f"6. If recognition fails, try `search_media` with keywords from the filename\n"
                f"7. Re-transfer using `transfer_file` with the source path and any identified media info (tmdbid, media_type)\n"
                f"8. Report the final result\n\n"
                f"IMPORTANT: This is a background system task, NOT a user conversation. "
                f"Your final response will be broadcast as a notification. "
                f"Only output a brief result summary. "
                f"Do NOT include greetings, explanations, or conversational text. "
                f"Respond in Chinese (中文)."
            )
        else:
            # 多条记录，使用批量处理逻辑
            retry_message = (
                f"[System Task - Batch Transfer Failed Retry] Multiple file transfers from the same source "
                f"have failed. These files likely belong to the SAME media (e.g., multiple episodes of the same TV show). "
                f"Please use the 'transfer-failed-retry' skill to retry them efficiently.\n\n"
                f"Failed transfer history record IDs: {ids_str}\n"
                f"Total failed records: {len(history_ids)}\n\n"
                f"Follow these steps:\n"
                f"1. Use `query_transfer_history` with status='failed' to find ALL records with these IDs "
                f"and understand the failure details\n"
                f"2. Since these files are likely from the same media, analyze the FIRST record to determine "
                f"the media identity and the best retry strategy. The root cause is usually the same for all files.\n"
                f"3. If the error is about media recognition (e.g., '未识别到媒体信息'), identify the media ONCE "
                f"using `recognize_media` or `search_media`, then reuse that result (tmdbid, media_type) for all files\n"
                f"4. For EACH failed record:\n"
                f"   a. Delete the failed history record using `delete_transfer_history`\n"
                f"   b. Re-transfer using `transfer_file` with the source path and the identified media info\n"
                f"5. Report a summary of results (how many succeeded, how many failed)\n\n"
                f"IMPORTANT OPTIMIZATION: These files share the same media identity. "
                f"Do NOT call `recognize_media` or `search_media` repeatedly for each file. "
                f"Identify the media ONCE, then apply to all files.\n\n"
                f"IMPORTANT: This is a background system task, NOT a user conversation. "
                f"Your final response will be broadcast as a notification. "
                f"Only output a brief result summary. "
                f"Do NOT include greetings, explanations, or conversational text. "
                f"Respond in Chinese (中文)."
            )

        try:

            await self.process_message(
                session_id=session_id,
                user_id=user_id,
                message=retry_message,
                channel=None,
                source=None,
                username=settings.SUPERUSER,
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

            logger.info(
                f"智能体重试整理：批量处理完成 IDs=[{ids_str}] (group={group_key})"
            )

            # 用完即弃，清理资源
            await self.clear_session(session_id, user_id)

        except Exception as e:
            logger.error(
                f"智能体重试整理失败 (IDs=[{ids_str}], group={group_key}): {e}"
            )


# 全局智能体管理器实例
agent_manager = AgentManager()
