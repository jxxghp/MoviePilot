from app.application.workflow import configure_workflow_runtime
from app.workflow import WorkflowManager

# 启动模块是 concrete WorkflowManager 的唯一宿主装配边界。
configure_workflow_runtime(lambda: WorkflowManager())


def init_workflow():
    """
    初始化工作流
    """
    WorkflowManager()


def stop_workflow() -> bool:
    """
    停止工作流并返回全部活动执行是否收敛。
    """
    return WorkflowManager().stop()
