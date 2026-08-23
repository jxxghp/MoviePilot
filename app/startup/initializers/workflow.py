from app.application.workflow import configure_workflow_runtime
from app.workflow import WorkFlowManager


# 启动模块是 concrete WorkFlowManager 的唯一宿主装配边界。
configure_workflow_runtime(lambda: WorkFlowManager())


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
