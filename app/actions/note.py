from app.actions import BaseAction
from app.schemas import ActionContext


class NoteAction(BaseAction):
    """
    备注
    """

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "备注"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "给工作流添加备注"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return {}

    @property
    def success(self) -> bool:
        return True

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        return context
