import asyncio
import threading

from app.agent import agent_manager
from app.core.config import settings
from app.log import logger


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


def init_agent():
    """
    初始化AI智能体（同步版本，用于在后台线程中运行）
    """
    try:
        if not settings.AI_AGENT_ENABLE:
            logger.info("AI智能体功能未启用")
            return True

        # 在新的事件循环中初始化AI智能体管理器
        def run_init():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(agent_initializer.initialize())
                if success:
                    logger.info("AI智能体管理器初始化成功")
                else:
                    logger.error("AI智能体管理器初始化失败")
                return success
            except Exception as err:
                logger.error(f"AI智能体管理器初始化失败: {err}")
                return False
            finally:
                loop.close()

        # 在后台线程中初始化
        init_thread = threading.Thread(target=run_init, daemon=True)
        init_thread.start()

        return True

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
