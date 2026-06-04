import threading
from time import monotonic, sleep
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import global_vars
from app.core.event import eventmanager, Event
from app.db.models import Workflow
from app.db.workflow_oper import WorkflowOper
from app.helper.module import ModuleHelper
from app.log import logger
from app.schemas import ActionContext, Action, ActionResult
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
            return obj.__module__.startswith("app.workflow.actions")

        # 加载所有动作
        self._actions = {}
        actions = ModuleHelper.load(
            "app.workflow.actions",
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
        for event_type_str in list(self._event_workflows.keys()):
            self.remove_workflow_event(event_type_str=event_type_str)
        self._actions = {}
        self._event_workflows = {}

    def execute(self, workflow_id: int, action: Action, context: ActionContext = None,
                inputs: Optional[dict] = None, runtime: Optional[dict] = None,
                cancel_token: Optional[Any] = None) -> ActionResult:
        """
        执行工作流动作
        """
        if not context:
            context = ActionContext()
        if action.type not in self._actions:
            logger.error(f"未找到动作: {action.type} - {action.name}")
            return ActionResult(success=False, message=" ", context=context)

        retry_config = self._get_retry_config(action)
        max_attempts = retry_config["max_attempts"]
        interval = retry_config["interval"]
        backoff = retry_config["backoff"]
        action_result = ActionResult(success=False, message="", context=context)

        for attempt in range(1, max_attempts + 1):
            if self._is_cancelled(workflow_id, cancel_token):
                return ActionResult(success=False, message="工作流已取消", context=context)
            runtime_data = {
                **(runtime or {}),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "cancel_token": cancel_token,
            }
            action_result = self._execute_action_once(
                workflow_id=workflow_id,
                action=action,
                context=context,
                inputs=inputs or {},
                runtime=runtime_data,
                cancel_token=cancel_token
            )
            action_result.attempts = attempt
            context = action_result.context or context
            if action_result.success:
                logger.info(f"{action.name} 执行成功")
                return action_result
            if attempt < max_attempts and not self._is_cancelled(workflow_id, cancel_token):
                wait_seconds = interval * (backoff ** (attempt - 1))
                logger.info(f"{action.name} 执行失败，{wait_seconds} 秒后重试（{attempt}/{max_attempts}）...")
                self._sleep_with_cancel(workflow_id, wait_seconds, cancel_token)

        logger.error(f"{action.name} 执行失败！")
        return action_result

    def excute(self, workflow_id: int, action: Action,
               context: ActionContext = None) -> Tuple[bool, str, ActionContext]:
        """
        执行工作流动作，兼容历史拼写错误的方法名。
        """
        action_result = self.execute(workflow_id=workflow_id, action=action, context=context)
        return bool(action_result.success), action_result.message or "", action_result.context or context or ActionContext()

    @staticmethod
    def _normalize_action_result(result: Any, action_obj: Any, fallback_context: ActionContext) -> ActionResult:
        """
        将旧版动作上下文与新版结构化结果统一为动作执行结果。
        """
        if isinstance(result, ActionResult):
            result.context = result.context or fallback_context
            if result.message is None:
                result.message = action_obj.message
            return result
        return ActionResult(
            success=action_obj.success,
            message=action_obj.message,
            context=result or fallback_context
        )

    def _execute_action_once(self, workflow_id: int, action: Action, context: ActionContext,
                             inputs: dict, runtime: dict, cancel_token: Optional[Any]) -> ActionResult:
        action_obj = self._actions[action.type](action.id)
        logger.info(f"执行动作: {action.id} - {action.name}")
        try:
            action_result = self._run_action_with_loop(
                workflow_id=workflow_id,
                action=action,
                action_obj=action_obj,
                context=context,
                inputs=inputs,
                runtime=runtime,
                cancel_token=cancel_token
            )
        except Exception as err:
            logger.error(f"{action.name} 执行失败: {err}")
            return ActionResult(success=False, message=f"{err}", context=context)
        return action_result

    def _run_action_with_loop(self, workflow_id: int, action: Action, action_obj: Any,
                              context: ActionContext, inputs: dict, runtime: dict,
                              cancel_token: Optional[Any]) -> ActionResult:
        timeout = self._get_action_timeout(action)
        started_at = monotonic()
        action_result = self._call_action(
            workflow_id=workflow_id,
            action=action,
            action_obj=action_obj,
            context=context,
            inputs=inputs,
            runtime=runtime
        )
        loop = self._get_action_data_value(action, "loop")
        loop_interval = self._get_action_data_value(action, "loop_interval")
        while loop and loop_interval and not action_obj.done:
            if self._is_cancelled(workflow_id, cancel_token):
                return ActionResult(success=False, message="工作流已取消", context=action_result.context or context)
            if timeout and monotonic() - started_at >= timeout:
                return ActionResult(success=False, message=f"动作执行超时（{timeout}秒）", context=action_result.context or context)
            logger.info(f"{action.name} 等待 {loop_interval} 秒后继续执行 ...")
            self._sleep_with_cancel(workflow_id, loop_interval, cancel_token)
            if self._is_cancelled(workflow_id, cancel_token):
                return ActionResult(success=False, message="工作流已取消", context=action_result.context or context)
            logger.info(f"继续执行动作: {action.id} - {action.name}")
            action_result = self._call_action(
                workflow_id=workflow_id,
                action=action,
                action_obj=action_obj,
                context=action_result.context or context,
                inputs=inputs,
                runtime=runtime
            )
        return action_result

    def _call_action(self, workflow_id: int, action: Action, action_obj: Any,
                     context: ActionContext, inputs: dict, runtime: dict) -> ActionResult:
        if hasattr(action_obj, "execute_with_inputs"):
            result = action_obj.execute_with_inputs(workflow_id, action.data, inputs, runtime, context)
        else:
            result = action_obj.execute(workflow_id, action.data, context)
        return self._normalize_action_result(result, action_obj, context)

    @staticmethod
    def _get_action_data_value(action: Action, key: str) -> Any:
        data = action.data or {}
        return data.get(key) if isinstance(data, dict) else None

    def _get_action_timeout(self, action: Action) -> Optional[int]:
        timeout = action.timeout or self._get_action_data_value(action, "timeout")
        return int(timeout) if timeout else None

    def _get_retry_config(self, action: Action) -> dict:
        retry_config = action.retry or self._get_action_data_value(action, "retry") or {}
        if not isinstance(retry_config, dict):
            retry_config = {}
        return {
            "max_attempts": max(int(retry_config.get("max_attempts") or 1), 1),
            "interval": max(float(retry_config.get("interval") or 0), 0),
            "backoff": max(float(retry_config.get("backoff") or 1), 1),
        }

    @staticmethod
    def _is_cancelled(workflow_id: int, cancel_token: Optional[Any]) -> bool:
        if cancel_token and cancel_token.is_cancelled():
            return True
        return global_vars.is_workflow_stopped(workflow_id)

    def _sleep_with_cancel(self, workflow_id: int, seconds: float, cancel_token: Optional[Any]) -> None:
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            if self._is_cancelled(workflow_id, cancel_token):
                return
            sleep(min(0.1, deadline - monotonic()))

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

    def update_workflow_event(self, workflow: Workflow):
        """
        更新工作流事件触发器
        """
        # 工作流可能切换触发事件，先按工作流ID从所有事件映射中移除。
        self.remove_workflow_event(workflow_id=workflow.id)
        # 如果工作流是事件触发类型且未被禁用
        if workflow.trigger_type == "event" and workflow.state != 'P':
            # 注册事件触发器
            self.register_workflow_event(workflow.id, workflow.event_type)

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
            for workflow in workflows:
                self.update_workflow_event(workflow)
        except Exception as e:
            logger.error(f"加载事件触发工作流失败: {e}")

    def register_workflow_event(self, workflow_id: int, event_type_str: str):
        """
        注册工作流事件触发器
        """
        if not event_type_str:
            return
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            logger.error(f"无效的事件类型: {event_type_str}")
            return
        if event_type in EventType:
            with self._lock:
                if event_type.value not in self._event_workflows:
                    self._event_workflows[event_type.value] = []
                    eventmanager.add_event_listener(event_type, self._handle_event)
                # 记录工作流事件触发器
                if workflow_id not in self._event_workflows[event_type.value]:
                    self._event_workflows[event_type.value].append(workflow_id)
                logger.info(f"已注册工作流 {workflow_id} 事件触发器: {event_type.value}")

    def remove_workflow_event(self, workflow_id: Optional[int] = None, event_type_str: Optional[str] = None):
        """
        移除工作流事件触发器
        """
        event_type_values = [event_type_str] if event_type_str else list(self._event_workflows.keys())
        for event_type_value in event_type_values:
            try:
                event_type = EventType(event_type_value)
            except ValueError:
                logger.error(f"无效的事件类型: {event_type_value}")
                continue
            with self._lock:
                workflow_ids = self._event_workflows.get(event_type.value)
                if not workflow_ids:
                    continue
                if workflow_id is None:
                    workflow_ids.clear()
                elif workflow_id in workflow_ids:
                    workflow_ids.remove(workflow_id)
                if not workflow_ids:
                    self._event_workflows.pop(event_type.value, None)
                    eventmanager.remove_event_listener(event_type, self._handle_event)
                logger.info(f"已移除工作流 {workflow_id or ''} 事件触发器")

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
