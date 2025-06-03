from app.core.config import settings
from app.helper.memory import MemoryManager


def init_memory_manager():
    """
    初始化内存监控器
    """
    memory_manager = MemoryManager()
    # 设置内存阈值和启动监控
    memory_manager.set_threshold(settings.CACHE_CONF['memory'])
    memory_manager.start_monitoring()


def stop_memory_manager():
    """
    停止内存监控器
    """
    MemoryManager().stop_monitoring()
