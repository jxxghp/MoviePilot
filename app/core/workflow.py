from time import sleep
from typing import Dict, Any, Tuple, List

from app.core.config import global_vars
from app.helper.module import ModuleHelper
from app.log import logger
from app.schemas import Action, ActionContext
from app.utils.singleton import Singleton


class WorkFlowManager(metaclass=Singleton):
    """
    工作流管理器
    """

    # 所有动作定义
    _actions: Dict[str, Any] = {}

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
            try:
                self._actions[action.__name__] = action
            except Exception as err:
                logger.error(f"加载动作失败: {action.__name__} - {err}")

    def stop(self):
        """
        停止
        """
        pass

    def excute(self, workflow_id: int, action: Action,
               context: ActionContext = None) -> Tuple[bool, str, ActionContext]:
        """
        执行工作流动作
        """
        if not context:
            context = ActionContext()
        if action.type in self._actions:
            # 实例化之前，清理掉类对象的数据

            # 实例化
            action_obj = self._actions[action.type](action.id)
            # 执行
            logger.info(f"执行动作: {action.id} - {action.name}")
            try:
                result_context = action_obj.execute(workflow_id, action.data, context)
            except Exception as err:
                logger.error(f"{action.name} 执行失败: {err}")
                return False, f"{err}", context
            loop = action.data.get("loop")
            loop_interval = action.data.get("loop_interval")
            if loop and loop_interval:
                while not action_obj.done:
                    if global_vars.is_workflow_stopped(workflow_id):
                        break
                    # 等待
                    logger.info(f"{action.name} 等待 {loop_interval} 秒后继续执行 ...")
                    sleep(loop_interval)
                    # 执行
                    logger.info(f"继续执行动作: {action.id} - {action.name}")
                    result_context = action_obj.execute(workflow_id, action.data, result_context)
            if action_obj.success:
                logger.info(f"{action.name} 执行成功")
            else:
                logger.error(f"{action.name} 执行失败！")
            return action_obj.success, action_obj.message, result_context
        else:
            logger.error(f"未找到动作: {action.type} - {action.name}")
            return False, " ", context

    def list_actions(self) -> List[dict]:
        """
        获取所有动作
        """
        return [
            {
                "type": key,
                "name": action.name,
                "description": action.description,
                "data": {
                    "label": action.name,
                    **action.data
                }
            } for key, action in self._actions.items()
        ]
