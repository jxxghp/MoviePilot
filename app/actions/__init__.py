from abc import ABC, abstractmethod

from pydantic.main import BaseModel

from app.chain import ChainBase
from app.schemas import ActionContext, ActionParams


class ActionChain(ChainBase):
    pass


class BaseAction(BaseModel, ABC):
    """
    工作流动作基类
    """

    # 完成标志
    _done_flag = False

    def __init__(self):
        super().__init__()
        self.chain = ActionChain()

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
    def done(self) -> bool:
        """
        判断动作是否完成
        """
        return self._done_flag

    @property
    @abstractmethod
    def success(self) -> bool:
        """
        判断动作是否成功
        """
        pass

    def job_done(self):
        """
        标记动作完成
        """
        self._done_flag = True
