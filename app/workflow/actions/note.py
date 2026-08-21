from app.workflow.actions import BaseAction
from app.schemas.workflow import ActionContext


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
        return True

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        return context
