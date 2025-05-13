from scheduler import Scheduler


def init_scheduler():
    """
    初始化定时器
    """
    Scheduler()

def stop_scheduler():
    """
    停止定时器
    """
    Scheduler().stop()
