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
        def check_module(module: Any):
            """
            检查模块
            """
            if not hasattr(module, 'execute') or not hasattr(module, "name"):
                return False
            return True

        # 加载所有动作
        self._actions = {}
        actions = ModuleHelper.load(
            "app.actions",
            filter_func=lambda _, obj: check_module(obj)
        )
        for action in actions:
            logger.debug(f"加载动作: {action.__name__}")
            self._actions[action.__name__] = action

    def stop(self):
        """
        停止
        """
        pass
