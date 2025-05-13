from monitor import Monitor


def init_monitor():
    """
    初始化监控器
    """
    Monitor()


def stop_monitor():
    """
    停止监控器
    """
    Monitor().stop()
