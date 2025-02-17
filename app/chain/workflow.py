from typing import List

from app.chain import ChainBase
from app.schemas import Workflow


class WorkflowChain(ChainBase):
    """
    工作流链
    """

    def process(self, workflow_id: int) -> bool:
        """
        处理工作流
        """
        pass

    def get_workflows(self) -> List[Workflow]:
        """
        获取工作流列表
        """
        pass
