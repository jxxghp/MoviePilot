"""MoviePilot AI智能体实现"""

import asyncio
from typing import Dict, List, Any

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_community.callbacks import get_openai_callback
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage, ToolCall, ToolMessage, SystemMessage
from langchain_core.runnables.history import RunnableWithMessageHistory

from app.agent.callback import StreamingCallbackHandler
from app.agent.memory import ConversationMemoryManager
from app.agent.prompt import PromptManager
from app.agent.tools.factory import MoviePilotToolFactory
from app.chain import ChainBase
from app.core.config import settings
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import Notification


class AgentChain(ChainBase):
    pass


class MoviePilotAgent:
    """MoviePilot AI智能体"""

    def __init__(self, session_id: str, user_id: str = None,
                 channel: str = None, source: str = None, username: str = None):
        self.session_id = session_id
        self.user_id = user_id
        self.channel = channel  # 消息渠道
        self.source = source  # 消息来源
        self.username = username  # 用户名

        # 消息助手
        self.message_helper = MessageHelper()

        # 记忆管理器
        self.memory_manager = ConversationMemoryManager()

        # 提示词管理器
        self.prompt_manager = PromptManager()

        # 回调处理器
        self.callback_handler = StreamingCallbackHandler(
            session_id=session_id
        )

        # LLM模型
        self.llm = self._initialize_llm()

        # 工具
        self.tools = self._initialize_tools()

        # 提示词模板
        self.prompt = self._initialize_prompt()

        # Agent执行器
        self.agent_executor = self._create_agent_executor()

    def _initialize_llm(self):
        """初始化LLM模型"""
        provider = settings.LLM_PROVIDER.lower()
        api_key = settings.LLM_API_KEY

        if provider == "google":
            if settings.PROXY_HOST:
                from langchain_openai import ChatOpenAI
                return ChatOpenAI(
                    model=settings.LLM_MODEL,
                    api_key=api_key,
                    max_retries=3,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    temperature=settings.LLM_TEMPERATURE,
                    streaming=True,
                    callbacks=[self.callback_handler],
                    stream_usage=True,
                    openai_proxy=settings.PROXY_HOST
                )
            else:
                from langchain_google_genai import ChatGoogleGenerativeAI
                return ChatGoogleGenerativeAI(
                    model=settings.LLM_MODEL,
                    google_api_key=api_key,
                    max_retries=3,
                    temperature=settings.LLM_TEMPERATURE,
                    streaming=True,
                    callbacks=[self.callback_handler]
                )
        elif provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek
            return ChatDeepSeek(
                model=settings.LLM_MODEL,
                api_key=api_key,
                max_retries=3,
                temperature=settings.LLM_TEMPERATURE,
                streaming=True,
                callbacks=[self.callback_handler],
                stream_usage=True
            )
        else:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=api_key,
                max_retries=3,
                base_url=settings.LLM_BASE_URL,
                temperature=settings.LLM_TEMPERATURE,
                streaming=True,
                callbacks=[self.callback_handler],
                stream_usage=True,
                openai_proxy=settings.PROXY_HOST
            )

    def _initialize_tools(self) -> List:
        """初始化工具列表"""
        return MoviePilotToolFactory.create_tools(
            session_id=self.session_id,
            user_id=self.user_id,
            channel=self.channel,
            source=self.source,
            username=self.username,
            callback_handler=self.callback_handler,
            memory_mananger=self.memory_manager
        )

    @staticmethod
    def _initialize_session_store() -> Dict[str, InMemoryChatMessageHistory]:
        """初始化内存存储"""
        return {}

    def get_session_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """获取会话历史"""
        chat_history = InMemoryChatMessageHistory()
        messages: List[dict] = self.memory_manager.get_recent_messages_for_agent(
            session_id=session_id,
            user_id=self.user_id
        )
        if messages:
            for msg in messages:
                if msg.get("role") == "user":
                    chat_history.add_message(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "agent":
                    chat_history.add_message(AIMessage(content=msg.get("content", "")))
                elif msg.get("role") == "tool_call":
                    metadata = msg.get("metadata", {})
                    chat_history.add_message(
                        AIMessage(
                            content=msg.get("content", ""),
                            tool_calls=[
                                ToolCall(
                                    id=metadata.get("call_id"),
                                    name=metadata.get("tool_name"),
                                    args=metadata.get("parameters"),
                                )
                            ]
                        )
                    )
                elif msg.get("role") == "tool_result":
                    chat_history.add_message(ToolMessage(content=msg.get("content", "")))
                elif msg.get("role") == "system":
                    chat_history.add_message(SystemMessage(content=msg.get("content", "")))
        return chat_history

    @staticmethod
    def _initialize_prompt() -> ChatPromptTemplate:
        """初始化提示词模板"""
        try:
            prompt_template = ChatPromptTemplate.from_messages([
                ("system", "{system_prompt}"),
                MessagesPlaceholder(variable_name="chat_history"),
                ("user", "{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ])
            logger.info("LangChain提示词模板初始化成功")
            return prompt_template
        except Exception as e:
            logger.error(f"初始化提示词失败: {e}")
            raise e

    def _create_agent_executor(self) -> RunnableWithMessageHistory:
        """创建Agent执行器"""
        try:
            agent = create_openai_tools_agent(
                llm=self.llm,
                tools=self.tools,
                prompt=self.prompt
            )
            executor = AgentExecutor(
                agent=agent,
                tools=self.tools,
                verbose=settings.LLM_VERBOSE,
                max_iterations=settings.LLM_MAX_ITERATIONS,
                return_intermediate_steps=True,
                handle_parsing_errors=True,
                early_stopping_method="force"
            )
            return RunnableWithMessageHistory(
                executor,
                self.get_session_history,
                input_messages_key="input",
                history_messages_key="chat_history"
            )
        except Exception as e:
            logger.error(f"创建Agent执行器失败: {e}")
            raise e

    async def process_message(self, message: str) -> str:
        """处理用户消息"""
        try:
            # 添加用户消息到记忆
            await self.memory_manager.add_memory(
                self.session_id,
                user_id=self.user_id,
                role="user",
                content=message
            )

            # 构建输入上下文
            input_context = {
                "system_prompt": self.prompt_manager.get_agent_prompt(channel=self.channel),
                "input": message
            }

            # 执行Agent
            logger.info(f"Agent执行推理: session_id={self.session_id}, input={message}")
            await self._execute_agent(input_context)

            # 获取Agent回复
            agent_message = await self.callback_handler.get_message()

            # 发送Agent回复给用户（通过原渠道）
            if agent_message:
                # 发送回复
                await self.send_agent_message(agent_message)

                # 添加Agent回复到记忆
                await self.memory_manager.add_memory(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    role="agent",
                    content=agent_message
                )
            else:
                agent_message = "很抱歉，智能体出错了，未能生成回复内容。"
                await self.send_agent_message(agent_message)

            return agent_message

        except Exception as e:
            error_message = f"处理消息时发生错误: {str(e)}"
            logger.error(error_message)
            # 发送错误消息给用户（通过原渠道）
            await self.send_agent_message(error_message)
            return error_message

    async def _execute_agent(self, input_context: Dict[str, Any]) -> Dict[str, Any]:
        """执行LangChain Agent"""
        try:
            with get_openai_callback() as cb:
                result = await self.agent_executor.ainvoke(
                    input_context,
                    config={"configurable": {"session_id": self.session_id}},
                    callbacks=[self.callback_handler]
                )
                logger.info(f"LLM调用消耗: \n{cb}")

                if cb.total_tokens > 0:
                    result["token_usage"] = {
                        "prompt_tokens": cb.prompt_tokens,
                        "completion_tokens": cb.completion_tokens,
                        "total_tokens": cb.total_tokens
                    }
            return result
        except asyncio.CancelledError:
            logger.info(f"Agent执行被取消: session_id={self.session_id}")
            return {
                "output": "任务已取消",
                "intermediate_steps": [],
                "token_usage": {}
            }
        except Exception as e:
            logger.error(f"Agent执行失败: {e}")
            return {
                "output": f"执行过程中发生错误: {str(e)}",
                "intermediate_steps": [],
                "token_usage": {}
            }

    async def send_agent_message(self, message: str, title: str = "MoviePilot助手"):
        """通过原渠道发送消息给用户"""
        await AgentChain().async_post_message(
            Notification(
                channel=self.channel,
                source=self.source,
                userid=self.user_id,
                username=self.username,
                title=title,
                text=message
            )
        )

    async def cleanup(self):
        """清理智能体资源"""
        logger.info(f"MoviePilot智能体已清理: session_id={self.session_id}")


class AgentManager:
    """AI智能体管理器"""

    def __init__(self):
        self.active_agents: Dict[str, MoviePilotAgent] = {}
        self.memory_manager = ConversationMemoryManager()

    async def initialize(self):
        """初始化管理器"""
        await self.memory_manager.initialize()

    async def close(self):
        """关闭管理器"""
        await self.memory_manager.close()
        # 清理所有活跃的智能体
        for agent in self.active_agents.values():
            await agent.cleanup()
        self.active_agents.clear()

    async def process_message(self, session_id: str, user_id: str, message: str,
                              channel: str = None, source: str = None, username: str = None) -> str:
        """处理用户消息"""
        # 获取或创建Agent实例
        if session_id not in self.active_agents:
            logger.info(f"创建新的AI智能体实例，session_id: {session_id}, user_id: {user_id}")
            agent = MoviePilotAgent(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                source=source,
                username=username
            )
            agent.memory_manager = self.memory_manager
            self.active_agents[session_id] = agent
        else:
            agent = self.active_agents[session_id]
            agent.user_id = user_id  # 确保user_id是最新的
            # 更新渠道信息
            if channel:
                agent.channel = channel
            if source:
                agent.source = source
            if username:
                agent.username = username

        # 处理消息
        return await agent.process_message(message)

    async def clear_session(self, session_id: str, user_id: str):
        """清空会话"""
        if session_id in self.active_agents:
            agent = self.active_agents[session_id]
            await agent.cleanup()
            del self.active_agents[session_id]
            await self.memory_manager.clear_memory(session_id, user_id)
            logger.info(f"会话 {session_id} 的记忆已清空")


# 全局智能体管理器实例
agent_manager = AgentManager()
