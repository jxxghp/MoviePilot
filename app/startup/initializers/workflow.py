from app.workflow import WorkFlowManager


def init_workflow():
    """
    初始化工作流
    """
    WorkFlowManager()


def stop_workflow():
    """
    停止工作流
    """
    WorkFlowManager().stop()
