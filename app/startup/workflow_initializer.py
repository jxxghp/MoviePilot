from app.core.workflow import WorkFlowManager
from app.chain.workflow import WorkflowChain


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
