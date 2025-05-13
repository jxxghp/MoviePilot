from app.core.workflow import WorkFlowManager


def init_workflow():
    """
    初始化动作
    """
    WorkFlowManager()


def stop_workflow():
    """
    停止动作
    """
    WorkFlowManager().stop()
