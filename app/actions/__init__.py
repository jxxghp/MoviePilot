from abc import ABC, abstractmethod

from pydantic.main import BaseModel

from app.schemas import ActionContext, ActionParams


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
    def execute(self, params: ActionParams, context: ActionContext) -> ActionContext:
        """
        执行动作
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def done(self) -> bool:
        """
        判断动作是否完成
        """
        pass

    @property
    @abstractmethod
    def success(self) -> bool:
        """
        判断动作是否成功
        """
        pass
