from app.core.config import settings
from app.helper.memory import MemoryHelper
from app.helper.diag_memory import DiagMemoryHelper
from app.log import logger


def init_memory_manager():
    """
    初始化内存管理和诊断工具。

    - MemoryHelper (psutil) 默认根据配置开启，用于宏观监控和缓解。
    - DiagMemoryHelper (tracemalloc) 仅在 MEMORY_DIAGNOSTICS 启动时生效，用于微观诊断。
    """
    memory_manager = MemoryHelper()
    # 设置内存阈值和启动监控
    memory_manager.set_threshold(settings.CONF['memory'])
    memory_manager.start_monitoring()

    if settings.MEMORY_DIAGNOSTICS:
        try:
            DiagMemoryHelper().start_monitoring()
        except Exception as e:
            logger.error(f"启动内存诊断工具失败: {e}")


def stop_memory_manager():
    """
    停止内存监控器
    """
    MemoryHelper().stop_monitoring()
