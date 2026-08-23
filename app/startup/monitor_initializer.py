from app.monitor import Monitor
from app.runtime.execution import run_in_threadpool_to_completion


def init_monitor() -> None:
    """初始化监控器；复用单例时必须显式开启新的应用 lifespan。"""
    monitor = Monitor.get_existing_instance()
    if monitor is None:
        Monitor()
        return
    if not monitor.lifecycle_closed:
        return
    if not monitor.reopen(timeout=Monitor.RELOAD_STOP_TIMEOUT):
        raise RuntimeError("旧目录监控 owner 未收敛，无法开启新生命周期")
    if not monitor.init(timeout=Monitor.RELOAD_STOP_TIMEOUT):
        raise RuntimeError("目录监控初始化失败")


async def stop_monitor(timeout: float = Monitor.LIFECYCLE_CLOSE_TIMEOUT) -> bool:
    """在线程池里永久关闭监控器，取消后仍等待同步 owner 收敛到终态。"""
    monitor = Monitor.get_existing_instance()
    if monitor is None:
        return True
    return bool(
        await run_in_threadpool_to_completion(monitor.close, timeout=timeout)
    )
