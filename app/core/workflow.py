from time import sleep
from typing import Dict, Any, Tuple

from app.actions import BaseAction
from app.helper.module import ModuleHelper
from app.log import logger
from app.schemas import Action, ActionContext
from app.utils.singleton import Singleton


class WorkFlowManager(metaclass=Singleton):
    """
    工作流管理器
    """

    # 所有动作定义
    _actions: Dict[str, BaseAction] = {}

    def __init__(self):
        self.init()

    def init(self):
        """
        初始化
        """

        def filter_func(obj: Any):
            """
            过滤函数，确保只加载新定义的类
            """
            if not isinstance(obj, type):
                return False
            if not hasattr(obj, 'execute') or not hasattr(obj, "name"):
                return False
            if obj.__name__ == "BaseAction":
                return False
            return obj.__module__.startswith("app.actions")

        # 加载所有动作
        self._actions = {}
        actions = ModuleHelper.load(
            "app.actions",
            filter_func=lambda _, obj: filter_func(obj)
        )
        for action in actions:
            logger.debug(f"加载动作: {action.__name__}")
            self._actions[action.__name__] = action

    def stop(self):
        """
        停止
        """
        pass

    def excute(self, action: Action, context: ActionContext = None) -> Tuple[bool, ActionContext]:
        """
        执行工作流动作
        """
        if not context:
            context = ActionContext()
        if action.id in self._actions:
            action_obj = self._actions[action.id]
            logger.info(f"执行动作: {action.id} - {action.name}")
            result_context = action_obj.execute(action.params, context)
            logger.info(f"{action.name} 执行结果: {action_obj.success}")
            if action.loop and action.loop_interval:
                while not action_obj.done:
                    logger.info(f"{action.name} 等待 {action.loop_interval} 秒后继续执行")
                    sleep(action.loop_interval)
                    logger.info(f"继续执行动作: {action.id} - {action.name}")
                    result_context = action_obj.execute(action.params, result_context)
                    logger.info(f"{action.name} 执行结果: {action_obj.success}")
            logger.info(f"{action.name} 执行完成")
            return action_obj.success, result_context
        else:
            logger.error(f"未找到动作: {action.id} - {action.name}")
            return False, context
