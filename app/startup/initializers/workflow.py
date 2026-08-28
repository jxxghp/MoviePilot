from app.application.workflow import configure_workflow_runtime, reset_workflow_runtime
from app.workflow import WorkflowManager


def configure_workflow_ports() -> None:
    """在工作流生命周期启动阶段登记 concrete manager provider。"""
    configure_workflow_runtime(lambda: WorkflowManager())


def reset_workflow_ports() -> None:
    """清除工作流 manager provider，支持重复 lifespan。"""
    reset_workflow_runtime()


def init_workflow():
    """
    初始化工作流
    """
    configure_workflow_ports()
    try:
        WorkflowManager()
    except Exception:
        reset_workflow_ports()
        raise


def stop_workflow() -> bool:
    """
    停止工作流并返回全部活动执行是否收敛。
    """
    converged = WorkflowManager().stop()
    if converged is not False:
        reset_workflow_ports()
    return converged
