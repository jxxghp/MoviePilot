from typing import List

from app.chain import ChainBase
from app.core.workflow import WorkFlowManager
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.schemas import Workflow, ActionContext


class WorkflowChain(ChainBase):
    """
    工作流链
    """

    def __init__(self):
        super().__init__()
        self.workflowoper = WorkflowOper()
        self.workflowmanager = WorkFlowManager()

    def process(self, workflow_id: int) -> bool:
        """
        处理工作流
        """
        workflow = self.workflowoper.get(workflow_id)
        if not workflow:
            logger.warn(f"工作流 {workflow_id} 不存在")
            return False
        if not workflow.actions:
            logger.warn(f"工作流 {workflow.name} 无动作")
            return False
        logger.info(f"开始处理 {workflow.name}，共 {len(workflow.actions)} 个动作 ...")
        # 启用上下文
        context = ActionContext()
        self.workflowoper.start(workflow_id)
        for action in workflow.actions:
            state, context = self.workflowmanager.excute(action, context)
            self.workflowoper.step(workflow_id, action=action.name, context=context.dict())
            if not state:
                logger.error(f"动作 {action.name} 执行失败，工作流失败")
                self.workflowoper.fail(workflow_id, result=f"动作 {action.name} 执行失败")
                return False
        logger.info(f"工作流 {workflow.name} 执行完成")
        self.workflowoper.success(workflow_id)
        return True

    def get_workflows(self) -> List[Workflow]:
        """
        获取工作流列表
        """
        return self.workflowoper.list_enabled()
