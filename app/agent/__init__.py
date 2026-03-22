import asyncio
import traceback
from time import strftime
from typing import Dict, List

from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
)
from langchain_core.messages import (
    HumanMessage,
    BaseMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.callback import StreamingHandler
from app.agent.memory import memory_manager
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.patch_tool_calls import PatchToolCallsMiddleware
from app.agent.prompt import prompt_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.chain import ChainBase
from app.core.config import settings
from app.helper.llm import LLMHelper
from app.log import logger
from app.schemas import Notification


class AgentChain(ChainBase):
    pass


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

        # 流式token管理
        self.stream_handler = StreamingHandler()

    @staticmethod
    def _initialize_llm():
        """
        初始化 LLM（带流式回调）
        """
        return LLMHelper.get_llm(streaming=True)

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
        )

    def _create_agent(self):
        """
        创建 LangGraph Agent（使用 create_agent + SummarizationMiddleware）
        """
        try:
            # 系统提示词
            system_prompt = prompt_manager.get_agent_prompt(
                channel=self.channel
            ).format(
                current_date=strftime('%Y-%m-%d')
            )

            # LLM 模型（用于 agent 执行）
            llm = self._initialize_llm()

            # 工具列表
            tools = self._initialize_tools()

            # 中间件
            middlewares = [
                # 记忆管理
                MemoryMiddleware(
                    sources=[str(settings.CONFIG_PATH / "agent" / "MEMORY.md")]
                ),
                # 上下文压缩
                SummarizationMiddleware(
                    model=llm,
                    trigger=("fraction", 0.85)
                ),
                # 错误工具调用修复
                PatchToolCallsMiddleware()
            ]

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

    async def process(self, message: str) -> str:
        """
        处理用户消息，流式推理并返回 Agent 回复
        """
        try:
            logger.info(f"Agent推理: session_id={self.session_id}, input={message}")

            # 获取历史消息
            messages = memory_manager.get_agent_messages(
                session_id=self.session_id,
                user_id=self.user_id
            )

            # 增加用户消息
            messages.append(HumanMessage(content=message))

            # 执行推理
            await self._execute_agent(messages)

        except Exception as e:
            error_message = f"处理消息时发生错误: {str(e)}"
            logger.error(error_message)
            await self.send_agent_message(error_message)
            return error_message

    async def _execute_agent(self, messages: List[BaseMessage]):
        """
        调用 LangGraph Agent，通过 astream_events 流式获取 token，
        同时用 UsageMetadataCallbackHandler 统计 token 用量。
        """
        try:
            # Agent运行配置
            agent_config = {
                "configurable": {
                    "thread_id": self.session_id,
                }
            }

            # 创建智能体
            agent = self._create_agent()

            # 流式运行智能体
            async for chunk in agent.astream(
                    {"messages": messages},
                    stream_mode="messages",
                    config=agent_config
            ):
                token, metadata = chunk
                # 处理流式token（过滤工具调用token，只保留模型生成的内容）
                if token and hasattr(token, "tool_call_chunks") and not token.tool_call_chunks:
                    self.stream_handler.emit(token.content)

            # 发送最终消息给用户
            await self.send_agent_message(
                self.stream_handler.take()
            )

            # 保存消息
            memory_manager.save_agent_messages(
                session_id=self.session_id,
                user_id=self.user_id,
                messages=agent.get_state(agent_config).values.get("messages", [])
            )

        except asyncio.CancelledError:
            logger.info(f"Agent执行被取消: session_id={self.session_id}")
            return "任务已取消", {}
        except Exception as e:
            logger.error(f"Agent执行失败: {e} - {traceback.format_exc()}")
            return str(e), {}

    async def send_agent_message(self, message: str, title: str = "MoviePilot助手"):
        """
        通过原渠道发送消息给用户
        """
        await AgentChain().async_post_message(
            Notification(
                channel=self.channel,
                source=self.source,
                userid=self.user_id,
                username=self.username,
                title=title,
                text=message,
            )
        )

    async def cleanup(self):
        """
        清理智能体资源
        """
        logger.info(f"MoviePilot智能体已清理: session_id={self.session_id}")


class AgentManager:
    """
    AI智能体管理器
    """

    def __init__(self):
        self.active_agents: Dict[str, MoviePilotAgent] = {}

    @staticmethod
    async def initialize():
        """
        初始化管理器
        """
        await memory_manager.initialize()

    async def close(self):
        """
        关闭管理器
        """
        await memory_manager.close()
        for agent in self.active_agents.values():
            await agent.cleanup()
        self.active_agents.clear()

    async def process_message(
            self,
            session_id: str,
            user_id: str,
            message: str,
            channel: str = None,
            source: str = None,
            username: str = None,
    ) -> str:
        """
        处理用户消息
        """
        if session_id not in self.active_agents:
            logger.info(
                f"创建新的AI智能体实例，session_id: {session_id}, user_id: {user_id}"
            )
            agent = MoviePilotAgent(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                source=source,
                username=username,
            )
            self.active_agents[session_id] = agent
        else:
            agent = self.active_agents[session_id]
            agent.user_id = user_id
            if channel:
                agent.channel = channel
            if source:
                agent.source = source
            if username:
                agent.username = username

        return await agent.process(message)

    async def clear_session(self, session_id: str, user_id: str):
        """
        清空会话
        """
        if session_id in self.active_agents:
            agent = self.active_agents[session_id]
            await agent.cleanup()
            del self.active_agents[session_id]
            await memory_manager.clear_memory(session_id, user_id)
            logger.info(f"会话 {session_id} 的记忆已清空")


# 全局智能体管理器实例
agent_manager = AgentManager()
