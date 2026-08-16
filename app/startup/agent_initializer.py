from app.agent.llm import AgentCapabilityManager, LLMHelper
from app.agent.orchestrator import agent_manager
from app.agent.prompt import prompt_manager
from app.agent.prompt.transfer_redo import build_manual_redo_prompt
from app.application.agent import register_agent_services
from app.runtime.config import settings
from app.runtime.log import logger

# 导入期即向 application 门面注册实现，保证任何先于 initialize 的
# 链层调用都能通过门面取到 Agent 服务对象。
register_agent_services(
    agent_manager=agent_manager,
    prompt_manager=prompt_manager,
    capability_manager=AgentCapabilityManager,
    llm_helper=LLMHelper,
    manual_redo_prompt_builder=build_manual_redo_prompt,
)


class AgentInitializer:
    """
    AI智能体初始化器
    """

    def __init__(self):
        self._initialized = False

    async def initialize(self) -> bool:
        """
        初始化AI智能体管理器
        """
        try:
            if not settings.AI_AGENT_ENABLE:
                logger.info("AI智能体功能未启用")
                return True

            await agent_manager.initialize()
            self._initialized = True
            logger.info("AI智能体管理器初始化成功")
            return True

        except Exception as e:
            logger.error(f"AI智能体管理器初始化失败: {e}")
            return False

    async def cleanup(self) -> None:
        """
        清理AI智能体管理器
        """
        try:
            if not self._initialized:
                return
            await agent_manager.close()
            self._initialized = False
            logger.info("AI智能体管理器已关闭")

        except Exception as e:
            logger.debug(f"关闭AI智能体管理器时发生错误: {e}")


# 全局AI智能体初始化器实例
agent_initializer = AgentInitializer()


async def init_agent() -> bool:
    """
    在应用事件循环中初始化AI智能体。
    """
    try:
        if not settings.AI_AGENT_ENABLE:
            logger.info("AI智能体功能未启用")
            return True

        return await agent_initializer.initialize()

    except Exception as e:
        logger.error(f"初始化AI智能体时发生错误: {e}")
        return False


async def stop_agent():
    """
    停止AI智能体（异步版本，用于在应用关闭时调用）
    """
    try:
        await agent_initializer.cleanup()
    except Exception as e:
        logger.error(f"停止AI智能体时发生错误: {e}")
