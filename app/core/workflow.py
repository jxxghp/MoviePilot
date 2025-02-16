from typing import Dict, Any

from app.actions import BaseAction
from app.helper.module import ModuleHelper
from app.log import logger
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
