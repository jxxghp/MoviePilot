from abc import ABC, abstractmethod

from pydantic.main import BaseModel

from app.schemas import ActionContext


class BaseAction(BaseModel, ABC):
    """
    工作流动作基类
    """

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    async def execute(self, params: dict, context: ActionContext) -> ActionContext:
        raise NotImplementedError
