import threading
from time import sleep
from typing import Dict, Any, Optional
from typing import List, Tuple

from app.core.config import global_vars
from app.core.event import eventmanager, Event
from app.db.workflow_oper import WorkflowOper
from app.helper.module import ModuleHelper
from app.log import logger
from app.schemas import ActionContext, Action
from app.schemas.types import EventType
from app.utils.singleton import Singleton


class WorkFlowManager(metaclass=Singleton):
    """
    工作流管理器
    """

    def __init__(self):
        # 所有动作定义
        self._lock = threading.Lock()
        self._actions: Dict[str, Any] = {}
        self._event_workflows: Dict[str, List[int]] = {}
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

        # 加载工作流事件触发器
        self.load_workflow_events()

    def stop(self):
        """
        停止
        """
        self._actions = {}
        self._event_workflows = {}

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

    def load_workflow_events(self, workflow_id: Optional[int] = None):
        """
        加载工作流触发事件
        """
        workflows = []
        if workflow_id:
            workflow = WorkflowOper().get(workflow_id)
            if workflow:
                workflows = [workflow]
        else:
            workflows = WorkflowOper().get_event_triggered_workflows()
        try:
            with self._lock:
                for workflow in workflows:
                    # 确保先移除旧的事件监听器
                    self.remove_workflow_event(workflow_id=workflow.id, event_type_str=workflow.event_type)
                    # 如果工作流是事件触发类型且未被禁用
                    if workflow.trigger_type == "event" and workflow.state != 'P':
                        # 注册事件触发器
                        self.register_workflow_event(workflow.id, workflow.event_type)
        except Exception as e:
            logger.error(f"加载事件触发工作流失败: {e}")

    def register_workflow_event(self, workflow_id: int, event_type_str: str):
        """
        注册工作流事件触发器
        """
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            logger.error(f"无效的事件类型: {event_type_str}")
            return
        if event_type in EventType:
            with self._lock:
                # 确保先移除旧的事件监听器
                self.remove_workflow_event(workflow_id, event_type.value)
                # 添加新的事件监听器
                eventmanager.add_event_listener(event_type, self._handle_event)
                # 记录工作流事件触发器
                if event_type.value not in self._event_workflows:
                    self._event_workflows[event_type.value] = []
                self._event_workflows[event_type.value].append(workflow_id)
                logger.info(f"已注册工作流 {workflow_id} 事件触发器: {event_type.value}")

    def remove_workflow_event(self, workflow_id: int, event_type_str: str):
        """
        移除工作流事件触发器
        """
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            logger.error(f"无效的事件类型: {event_type_str}")
            return
        if event_type in EventType:
            with self._lock:
                eventmanager.remove_event_listener(event_type, self._handle_event)
                if event_type.value in self._event_workflows:
                    if workflow_id in self._event_workflows[event_type.value]:
                        self._event_workflows[event_type.value].remove(workflow_id)
                        if not self._event_workflows[event_type.value]:
                            del self._event_workflows[event_type.value]
                logger.info(f"已移除工作流 {workflow_id} 事件触发器")

    def _handle_event(self, event: Event):
        """
        处理事件，触发相应的工作流
        """
        try:
            event_type_str = str(event.event_type.value)
            with self._lock:
                if event_type_str not in self._event_workflows:
                    return
                workflow_ids = self._event_workflows[event_type_str].copy()
            for workflow_id in workflow_ids:
                self._trigger_workflow(workflow_id, event)
        except Exception as e:
            logger.error(f"处理工作流事件失败: {e}")

    def _trigger_workflow(self, workflow_id: int, event: Event):
        """
        触发工作流执行
        """
        try:
            # 检查工作流是否存在且启用
            workflow = WorkflowOper().get(workflow_id)
            if not workflow or workflow.state == 'P':
                return

            # 检查事件条件
            if not self._check_event_conditions(workflow, event):
                logger.debug(f"工作流 {workflow.name} 事件条件不匹配，跳过执行")
                return

            # 检查工作流是否正在运行
            if workflow.state == 'R':
                logger.warning(f"工作流 {workflow.name} 正在运行中，跳过重复触发")
                return

            logger.info(f"事件 {event.event_type.value} 触发工作流: {workflow.name}")

            # 发送工作流执行事件以启动工作流
            eventmanager.send_event(EventType.WorkflowExecute, {
                "workflow_id": workflow_id,
            })

        except Exception as e:
            logger.error(f"触发工作流 {workflow_id} 失败: {e}")

    def _check_event_conditions(self, workflow, event: Event) -> bool:
        """
        检查事件是否满足工作流的触发条件
        """
        if not workflow.event_conditions:
            return True

        conditions = workflow.event_conditions
        event_data = event.event_data or {}

        # 检查字段匹配条件
        for field, expected_value in conditions.items():
            if field not in event_data:
                return False
            actual_value = event_data[field]
            # 支持多种条件匹配方式
            if isinstance(expected_value, dict):
                # 复杂条件匹配
                if not self._check_complex_condition(actual_value, expected_value):
                    return False
            else:
                # 简单值匹配
                if actual_value != expected_value:
                    return False
        return True

    @staticmethod
    def _check_complex_condition(actual_value: any, condition: dict) -> bool:
        """
        检查复杂条件匹配
        支持的操作符：equals, not_equals, contains, not_contains, in, not_in, regex
        """
        for operator, expected_value in condition.items():
            if operator == "equals":
                if actual_value != expected_value:
                    return False
            elif operator == "not_equals":
                if actual_value == expected_value:
                    return False
            elif operator == "contains":
                if expected_value not in str(actual_value):
                    return False
            elif operator == "not_contains":
                if expected_value in str(actual_value):
                    return False
            elif operator == "in":
                if actual_value not in expected_value:
                    return False
            elif operator == "not_in":
                if actual_value in expected_value:
                    return False
            elif operator == "regex":
                import re
                if not re.search(expected_value, str(actual_value)):
                    return False
        return True

    def get_event_workflows(self) -> dict:
        """
        获取所有事件触发的工作流
        """
        with self._lock:
            return self._event_workflows.copy()
