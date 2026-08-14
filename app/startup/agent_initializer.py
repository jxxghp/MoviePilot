from app.agent.orchestrator import agent_manager
from app.runtime.config import settings
from app.runtime.log import logger


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
