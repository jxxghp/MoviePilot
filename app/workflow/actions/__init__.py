from abc import ABC, abstractmethod
from typing import Union

from app.chain import ChainBase
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import ActionContext, ActionParams, ActionResult


class ActionChain(ChainBase):
    pass


class BaseAction(ABC):
    """
    工作流动作基类
    """

    # 动作ID
    _action_id = None
    # 完成标志
    _done_flag = False
    # 执行信息
    _message = ""
    # 缓存键值
    _cache_key = "WorkflowCache-%s"

    def __init__(self, action_id: str):
        self._action_id = action_id
        self._done_flag = False
        self._message = ""
        self.systemconfigoper = SystemConfigOper()

    @classmethod
    @property
    @abstractmethod
    def name(cls) -> str:  # noqa
        pass

    @classmethod
    @property
    @abstractmethod
    def description(cls) -> str:  # noqa
        pass

    @classmethod
    @property
    @abstractmethod
    def data(cls) -> dict:  # noqa
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

    def check_cache(self, workflow_id: int, key: str) -> bool:
        """
        检查是否处理过
        """
        workflow_key = self._cache_key % workflow_id
        workflow_cache = self.systemconfigoper.get(workflow_key) or {}
        action_cache = workflow_cache.get(self._action_id) or []
        return key in action_cache

    def save_cache(self, workflow_id: int, data: Union[list, str]):
        """
        保存缓存
        """
        workflow_key = self._cache_key % workflow_id
        workflow_cache = self.systemconfigoper.get(workflow_key) or {}
        action_cache = workflow_cache.get(self._action_id) or []
        if isinstance(data, list):
            for item in data:
                if item not in action_cache:
                    action_cache.append(item)
        else:
            if data not in action_cache:
                action_cache.append(data)
        workflow_cache[self._action_id] = action_cache
        self.systemconfigoper.set(workflow_key, workflow_cache)

    @abstractmethod
    def execute(self, workflow_id: int, params: ActionParams, context: ActionContext) -> ActionContext:
        """
        执行动作
        """
        raise NotImplementedError

    def execute_with_inputs(self, workflow_id: int, params: ActionParams, inputs: dict,
                            runtime: dict, context: ActionContext) -> ActionResult:
        """
        使用显式输入与运行期信息执行动作。
        """
        _ = inputs, runtime
        result_context = self.execute(workflow_id, params, context)
        return ActionResult(
            success=self.success,
            message=self.message,
            context=result_context
        )
