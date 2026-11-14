"""
在工作流执行记录中附加备注。
"""

from app.schemas.workflow import ActionContext
from app.workflow.actions import BaseAction


class NoteAction(BaseAction):
    """
    备注
    """

    contract = {}

    name = "备注"
    description = "给工作流添加备注"
    data = {}

    @property
    def success(self) -> bool:
        """
        返回当前动作是否已达到完成条件。
        """
        return True

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        备注不处理输入，原样保留工作流上下文。
        """
        return context
