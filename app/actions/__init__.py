from abc import ABC, abstractmethod

from app.chain import ChainBase
from app.schemas import ActionContext, ActionParams


class ActionChain(ChainBase):
    pass


class BaseAction(ABC):
    """
    工作流动作基类
    """

    # 完成标志
    _done_flag = False
    # 执行信息
    _message = ""

    @classmethod
    @property
    @abstractmethod
    def name(cls) -> str:
        pass

    @classmethod
    @property
    @abstractmethod
    def description(cls) -> str:
        pass

    @classmethod
    @property
    @abstractmethod
    def data(cls) -> dict:
        pass

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

    @property
    def message(self) -> str:
        """
        执行信息
        """
        return self._message

    def job_done(self, message: str = None):
        """
        标记动作完成
        """
        self._message = message
        self._done_flag = True

    @abstractmethod
    def execute(self, workflow_id: int, params: ActionParams, context: ActionContext) -> ActionContext:
        """
        执行动作
        """
        raise NotImplementedError
