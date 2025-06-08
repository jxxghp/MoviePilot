from app.helper.memory import MemoryHelper


def init_memory_manager():
    """
    初始化内存监控器
    """
    memory_manager = MemoryHelper()
    # 设置内存阈值和启动监控
    memory_manager.start_monitoring()


def stop_memory_manager():
    """
    停止内存监控器
    """
    MemoryHelper().stop_monitoring()
