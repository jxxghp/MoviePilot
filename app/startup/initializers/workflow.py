"""
装配工作流应用服务所需的运行时端口。
"""

from app.application.workflow import configure_workflow_runtime, reset_workflow_runtime
from app.workflow import WorkflowManager


def configure_workflow_ports() -> None:
    """
    在工作流启动阶段登记具体管理器的创建函数。
    """
    configure_workflow_runtime(lambda: WorkflowManager())


def reset_workflow_ports() -> None:
    """
    清除工作流管理器创建函数，以支持重复启动和关闭。
    """
    reset_workflow_runtime()


def init_workflow() -> None:
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
    converged: bool = WorkflowManager().stop()
    if converged is not False:
        reset_workflow_ports()
    return converged
