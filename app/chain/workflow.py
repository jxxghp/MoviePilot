import ast
import base64
import copy
import inspect
import pickle
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import sleep
from typing import Any, Callable, List, Optional, Tuple

from fastapi.encoders import jsonable_encoder

from app.chain import ChainBase
from app.core.config import global_vars
from app.core.event import Event, eventmanager
from app.db.models import Workflow
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.schemas import ActionContext, ActionFlow, Action, ActionExecution, ActionResult
from app.schemas.types import EventType
from app.workflow import WorkFlowManager


ARTIFACT_FIELDS = {"torrents", "medias", "fileitems", "downloads", "sites", "subscribes"}
DEFAULT_WORKFLOW_MAX_WORKERS = 4


class WorkflowCancelToken:
    """
    工作流取消令牌。
    """

    def __init__(self, workflow_id: int):
        """
        初始化取消令牌。
        :param workflow_id: 工作流ID
        """
        self.workflow_id = workflow_id

    def is_cancelled(self) -> bool:
        """
        判断工作流是否已被取消。
        """
        return global_vars.is_workflow_stopped(self.workflow_id)


class WorkflowExecutor:
    """
    工作流执行器
    """

    def __init__(self, workflow: Workflow, step_callback: Callable = None):
        """
        初始化工作流执行器
        :param workflow: 工作流对象
        :param step_callback: 步骤回调函数
        """
        # 工作流数据
        self.workflow = workflow
        self.step_callback = step_callback
        self.actions = {action['id']: Action(**action) for action in workflow.actions}
        self.flows = [ActionFlow(**flow) for flow in workflow.flows]
        self.execution_config = getattr(workflow, "execution_config", None) or {}
        self.restored_execution_state = getattr(workflow, "execution_state", None) or {}
        self.total_actions = len(self.actions)
        self.success = True
        self.has_failure = False
        self.stopped = False
        self.errmsg = ""
        self.errors = self.get_restored_errors()
        self.node_metadata = self.get_restored_node_metadata()
        self.node_attempts = self.get_restored_attempts()
        self.node_states = self.get_restored_node_states()
        self.completed_actions = {
            action_id for action_id, state in self.node_states.items()
            if state == "success"
        }
        self.finished_actions = len([
            state for state in self.node_states.values()
            if state in ("success", "failed", "skipped")
        ])
        self.flow_finished = set()
        self.flow_satisfied = set()
        self.flow_failed = set()

        # 工作流管理器
        self.workflowmanager = WorkFlowManager()
        # 线程安全队列
        self.queue = deque()
        self.queued_actions = set()
        self.active_concurrency_keys = set()
        # 锁用于保证线程安全
        self.lock = threading.Lock()
        # 线程池
        self.executor = ThreadPoolExecutor(max_workers=self.get_workflow_max_workers())
        self.cancel_token = WorkflowCancelToken(self.workflow.id)
        # 跟踪运行中的任务数
        self.running_tasks = 0

        # 构建出边与入边表，用于条件流转和多上游汇合。
        self.outgoing_flows = defaultdict(list)
        self.incoming_flows = defaultdict(list)
        for flow in self.flows:
            if not flow.source or not flow.target:
                continue
            self.outgoing_flows[flow.source].append(flow)
            self.incoming_flows[flow.target].append(flow)

        # 初始上下文
        self.context = self.restore_context()
        self.ensure_context_partitions()

        # 恢复工作流
        global_vars.workflow_resume(self.workflow.id)
        # 恢复时重新释放已终态节点的出边，使后继节点能继续执行或保持跳过传播。
        for action_id, state in self.node_states.items():
            if state == "success":
                self.release_successors(action_id, source_success=True)
            elif state in ("failed", "skipped"):
                self.release_successors(action_id, source_success=False)
        # 初始化队列，添加没有入边的起始节点。
        for action_id in self.actions:
            if self.node_states.get(action_id) == "pending" and not self.incoming_flows.get(action_id):
                self.enqueue_node(action_id)

    def get_workflow_max_workers(self) -> int:
        """
        获取工作流最大并发数。
        """
        max_workers = self.execution_config.get("max_workers") if isinstance(self.execution_config, dict) else None
        try:
            return max(int(max_workers or DEFAULT_WORKFLOW_MAX_WORKERS), 1)
        except (TypeError, ValueError):
            return DEFAULT_WORKFLOW_MAX_WORKERS

    def get_restored_node_metadata(self) -> dict:
        """
        获取已持久化的节点状态元数据。
        """
        nodes = self.restored_execution_state.get("nodes") if isinstance(self.restored_execution_state, dict) else {}
        return nodes if isinstance(nodes, dict) else {}

    def get_restored_errors(self) -> dict:
        """
        获取已持久化的错误状态。
        """
        errors = self.restored_execution_state.get("errors") if isinstance(self.restored_execution_state, dict) else {}
        return errors if isinstance(errors, dict) else {}

    def get_restored_attempts(self) -> dict:
        """
        获取已持久化的节点尝试次数。
        """
        attempts = {}
        for action_id, metadata in self.get_restored_node_metadata().items():
            if isinstance(metadata, dict) and metadata.get("attempt"):
                attempts[action_id] = int(metadata.get("attempt") or 0)
        return attempts

    def get_restored_node_states(self) -> dict:
        """
        获取结构化节点状态，兼容旧版 current_action 字符串。
        """
        legacy_actions = {
            action_id for action_id in (self.workflow.current_action or "").split(",")
            if action_id in self.actions
        }
        states = {}
        for action_id in self.actions:
            metadata = self.node_metadata.get(action_id) or {}
            state = metadata.get("state") if isinstance(metadata, dict) else None
            if state == "completed":
                state = "success"
            if state in ("running", "queued"):
                state = "pending"
            if not state and action_id in legacy_actions:
                state = "success"
            states[action_id] = state or "pending"
        return states

    def restore_context(self) -> ActionContext:
        """
        恢复工作流上下文，兼容旧版 Base64 Pickle 存储格式。
        """
        context = ActionContext()
        if self.workflow.current_action and self.workflow.context:
            logger.info(f"工作流已执行动作：{self.workflow.current_action}")
            try:
                decoded_data = base64.b64decode(self.workflow.context["content"])
                context = pickle.loads(decoded_data)
            except Exception as err:
                logger.error(f"工作流上下文恢复失败: {str(err)}")
                context = ActionContext()
        outputs = self.restored_execution_state.get("outputs") if isinstance(self.restored_execution_state, dict) else {}
        if outputs and not getattr(context, "node_outputs", None):
            context.node_outputs = outputs
        return context

    def ensure_context_partitions(self) -> None:
        """
        确保上下文具备新版分区结构，并把旧字段映射到 artifacts。
        """
        self.context.workflow_context = self.context.workflow_context or {}
        self.context.node_outputs = self.context.node_outputs or {}
        self.context.runtime_state = self.context.runtime_state or {}
        self.context.artifacts = self.context.artifacts or {}
        for key in ARTIFACT_FIELDS:
            value = getattr(self.context, key, None)
            if value not in (None, "", [], {}) and key not in self.context.artifacts:
                self.context.artifacts[key] = value
        self.update_runtime_state()

    def update_runtime_state(self) -> None:
        """
        更新上下文中的运行期状态分区。
        """
        self.context.runtime_state.update({
            "progress": self.context.progress,
            "finished_actions": self.finished_actions,
            "running_tasks": self.running_tasks,
            "errors": self.errors,
            "node_states": self.node_states,
            "attempts": self.node_attempts,
        })

    def set_node_state(self, action_id: str, state: str, message: Optional[str] = None) -> None:
        """
        更新节点结构化状态。
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        metadata = self.node_metadata.setdefault(action_id, {})
        metadata["state"] = state
        metadata["attempt"] = self.node_attempts.get(action_id, metadata.get("attempt") or 0)
        if state == "running":
            metadata["started_at"] = now
        if state in ("success", "failed", "skipped"):
            metadata["finished_at"] = now
        if message is not None:
            metadata["message"] = message
        self.node_states[action_id] = state
        self.update_runtime_state()

    def build_execution_state(self) -> dict:
        """
        构建可持久化的结构化执行状态。
        """
        self.update_runtime_state()
        return self.make_json_safe({
            "version": 1,
            "nodes": self.node_metadata,
            "outputs": self.context.node_outputs,
            "errors": self.errors,
            "runtime": self.context.runtime_state,
        })

    @staticmethod
    def make_json_safe(value: Any) -> Any:
        """
        将运行期对象转换为可写入 JSON 列的数据。
        """
        try:
            return jsonable_encoder(value)
        except Exception:
            return str(value)

    def execute(self) -> None:
        """
        执行工作流
        """
        try:
            while True:
                should_sleep = False
                node_id = None
                with self.lock:
                    if global_vars.is_workflow_stopped(self.workflow.id):
                        self.success = False
                        self.stopped = True
                        self.errmsg = "工作流已停止"
                        if self.running_tasks == 0:
                            break
                        should_sleep = True
                    # 退出条件：队列为空且无运行任务
                    elif not self.queue and self.running_tasks == 0:
                        break
                    # 出错后不再调度新节点，但等待已提交节点完成，避免后台线程继续写状态。
                    if not self.success:
                        if self.running_tasks == 0:
                            break
                        should_sleep = True
                    elif not self.queue:
                        should_sleep = True
                    else:
                        node_id = self.pop_dispatchable_node()
                        if not node_id:
                            should_sleep = True

                if should_sleep:
                    sleep(0.1)
                    continue

                if not node_id:
                    continue

                # 提交任务到线程池，每个节点使用上下文快照，避免并行节点互相修改同一个对象。
                future = self.executor.submit(
                    self.execute_node,
                    self.workflow.id,
                    node_id,
                    copy.deepcopy(self.context)
                )
                future.add_done_callback(self.on_node_complete)
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)

    def pop_dispatchable_node(self) -> Optional[str]:
        """
        从队列中取出当前可调度节点。
        """
        for _ in range(len(self.queue)):
            node_id = self.queue.popleft()
            self.queued_actions.discard(node_id)
            if self.node_states.get(node_id) != "queued":
                continue
            concurrency_key = self.get_action_concurrency_key(self.actions[node_id])
            if concurrency_key and concurrency_key in self.active_concurrency_keys:
                self.queue.append(node_id)
                self.queued_actions.add(node_id)
                continue
            if concurrency_key:
                self.active_concurrency_keys.add(concurrency_key)
            self.running_tasks += 1
            self.set_node_state(node_id, "running")
            return node_id
        return None

    def execute_node(self, workflow_id: int, node_id: str,
                     context: ActionContext) -> Tuple[Action, ActionResult]:
        """
        执行单个节点操作，返回修改后的上下文和节点ID
        """
        action = self.actions[node_id]
        action_result = self.workflowmanager.execute(
            workflow_id,
            action,
            context=context,
            inputs=self.build_action_inputs(action),
            runtime=self.build_action_runtime(action),
            cancel_token=self.cancel_token
        )
        return action, action_result

    def on_node_complete(self, future):
        """
        节点完成回调：更新上下文、处理后继节点
        """
        action = None
        try:
            action, action_result = future.result()
            with self.lock:
                if global_vars.is_workflow_stopped(self.workflow.id):
                    self.success = False
                    self.stopped = True
                    self.errmsg = "工作流已停止"
                    return
                state = bool(action_result.success)
                message = action_result.message or ""
                result_ctx = action_result.context or ActionContext()
                self.node_attempts[action.id] = action_result.attempts or self.node_attempts.get(action.id, 1)

                self.finished_actions += 1
                self.update_progress()
                # 更新当前进度
                self.context.execute_history.append(
                    ActionExecution(
                        action=action.name,
                        result=state,
                        message=message
                    )
                )

                # 节点执行失败时默认停止；显式配置 continue/ignore 时继续释放后续 all_done 汇合。
                if not state:
                    self.errors[action.id] = message or f"{action.name} 失败"
                    self.set_node_state(action.id, "failed", message=message)
                    fail_policy = self.get_action_fail_policy(action)
                    if fail_policy != "ignore":
                        self.has_failure = True
                        self.errmsg = f"{action.name} 失败"
                    if fail_policy == "stop":
                        self.success = False
                        self.call_step_callback(action, completed=False)
                        return
                    if fail_policy not in ("continue", "ignore"):
                        self.success = False
                        self.errmsg = f"{action.name} 失败：无效失败策略 {fail_policy}"
                        self.call_step_callback(action, completed=False)
                        return
                    self.release_successors(action.id, source_success=False)
                    self.call_step_callback(action, completed=False)
                    return

                self.ensure_result_context_partitions(result_ctx)
                outputs = self.normalize_action_outputs(action, action_result, result_ctx)
                self.merge_context_partitions(result_ctx)
                self.merge_action_outputs(action, outputs)
                self.record_node_outputs(action.id, outputs)
                self.completed_actions.add(action.id)
                self.set_node_state(action.id, "success", message=message)
                # 处理后继节点
                self.release_successors(action.id, source_success=True)
                # 回调
                self.call_step_callback(action, completed=True)
        except Exception as err:
            logger.error(f"工作流节点执行回调失败: {str(err)}")
            with self.lock:
                self.success = False
                self.errmsg = str(err)
        finally:
            # 标记任务完成
            with self.lock:
                if action:
                    concurrency_key = self.get_action_concurrency_key(action)
                    if concurrency_key:
                        self.active_concurrency_keys.discard(concurrency_key)
                self.running_tasks -= 1
                self.update_runtime_state()

    def enqueue_node(self, node_id: str) -> None:
        """
        将满足条件的节点加入待执行队列。
        """
        if node_id not in self.actions:
            return
        if self.node_states.get(node_id) != "pending" or node_id in self.queued_actions:
            return
        self.queue.append(node_id)
        self.queued_actions.add(node_id)
        self.set_node_state(node_id, "queued")

    def skip_node(self, node_id: str, message: str) -> None:
        """
        将不可达节点标记为跳过，并把跳过状态继续传递给后继节点。
        """
        if node_id not in self.actions:
            return
        if self.node_states.get(node_id) not in ("pending", "queued"):
            return
        self.queued_actions.discard(node_id)
        self.finished_actions += 1
        self.update_progress()
        self.set_node_state(node_id, "skipped", message=message)
        self.context.execute_history.append(
            ActionExecution(
                action=self.actions[node_id].name,
                result=True,
                message=message
            )
        )
        self.call_step_callback(self.actions[node_id], completed=False)
        self.release_successors(node_id, source_success=False)

    def release_successors(self, source_id: str, source_success: bool) -> None:
        """
        根据源节点状态释放出边，并重新判断目标节点是否可运行。
        """
        flows = self.outgoing_flows.get(source_id, [])
        branch_policy = self.get_action_branch_policy(self.actions.get(source_id), flows)
        matched_exclusive_flow = None
        for flow in flows:
            flow_key = self.get_flow_key(flow)
            if flow_key in self.flow_finished:
                continue
            condition_matched = False
            if source_success:
                try:
                    condition_matched = self.evaluate_condition(self.get_flow_condition(flow))
                except ValueError as err:
                    self.success = False
                    self.errmsg = f"流程条件判断失败：{err}"
                    return
                if branch_policy == "exclusive" and condition_matched and matched_exclusive_flow:
                    condition_matched = False
                elif branch_policy == "exclusive" and condition_matched:
                    matched_exclusive_flow = flow_key
            self.flow_finished.add(flow_key)
            if source_success and condition_matched:
                self.flow_satisfied.add(flow_key)
            if not source_success and self.node_states.get(source_id) == "failed":
                self.flow_failed.add(flow_key)
            self.evaluate_target_state(flow.target)

    def evaluate_target_state(self, target_id: str) -> None:
        """
        按目标节点汇合策略判断节点是否入队或跳过。
        """
        if not target_id or target_id not in self.actions:
            return
        if self.node_states.get(target_id) != "pending":
            return
        incoming_flows = self.incoming_flows.get(target_id, [])
        if not incoming_flows:
            self.enqueue_node(target_id)
            return

        total_count = len(incoming_flows)
        finished_count = sum(1 for flow in incoming_flows if self.get_flow_key(flow) in self.flow_finished)
        satisfied_count = sum(1 for flow in incoming_flows if self.get_flow_key(flow) in self.flow_satisfied)
        failed_count = sum(1 for flow in incoming_flows if self.get_flow_key(flow) in self.flow_failed)
        join_policy = self.get_action_join_policy(self.actions[target_id], incoming_flows)

        if join_policy == "fail_fast":
            if failed_count > 0:
                self.skip_node(target_id, "上游失败触发 fail_fast，已取消后续节点")
            elif finished_count == total_count and satisfied_count == total_count:
                self.enqueue_node(target_id)
            elif finished_count == total_count:
                self.skip_node(target_id, "上游条件未全部满足，已跳过")
            return

        if join_policy == "any_success":
            if satisfied_count > 0:
                self.enqueue_node(target_id)
            elif finished_count == total_count:
                self.skip_node(target_id, "所有上游条件均未满足，已跳过")
            return

        if join_policy == "all_done":
            if finished_count == total_count:
                self.enqueue_node(target_id)
            return

        if join_policy != "all_success":
            self.success = False
            self.errmsg = f"{self.actions[target_id].name} 汇合策略无效：{join_policy}"
            return

        if finished_count != total_count:
            return
        if satisfied_count == total_count:
            self.enqueue_node(target_id)
        else:
            self.skip_node(target_id, "上游条件未全部满足，已跳过")

    def update_progress(self) -> None:
        """
        根据已完成和已跳过节点数量更新整体进度。
        """
        self.context.progress = round(self.finished_actions / self.total_actions * 100) if self.total_actions else 100
        self.update_runtime_state()

    def build_action_inputs(self, action: Action) -> dict:
        """
        根据动作输入声明读取上游节点输出。
        """
        inputs = {}
        input_paths = action.inputs or self.get_action_data_value(action, "inputs") or []
        if isinstance(input_paths, str):
            input_paths = [item.strip() for item in input_paths.splitlines() if item.strip()]
        for input_path in input_paths:
            inputs[input_path] = self.resolve_context_path(input_path)
        return inputs

    def build_action_runtime(self, action: Action) -> dict:
        """
        构建传递给动作的新运行期数据。
        """
        return {
            "workflow_id": self.workflow.id,
            "action_id": action.id,
            "execution_config": self.execution_config,
            "runtime_state": self.context.runtime_state,
        }

    def ensure_result_context_partitions(self, context: ActionContext) -> None:
        """
        确保动作返回上下文具备新版分区字段。
        """
        context.workflow_context = context.workflow_context or {}
        context.node_outputs = context.node_outputs or {}
        context.runtime_state = context.runtime_state or {}
        context.artifacts = context.artifacts or {}

    def normalize_action_outputs(self, action: Action, action_result: ActionResult,
                                 result_context: ActionContext) -> dict:
        """
        根据动作输出声明整理当前节点输出。
        """
        outputs = action_result.outputs or self.extract_context_outputs(result_context)
        declared_outputs = action.outputs or self.get_action_data_value(action, "outputs")
        if isinstance(declared_outputs, list):
            return {key: outputs.get(key) for key in declared_outputs if outputs.get(key) not in (None, "", [], {})}
        if isinstance(declared_outputs, dict):
            return {
                key: outputs.get(key)
                for key in declared_outputs
                if outputs.get(key) not in (None, "", [], {})
            } or outputs
        return outputs

    def record_node_outputs(self, action_id: str, outputs: dict) -> None:
        """
        记录当前节点输出，供后续条件表达式读取。
        """
        if outputs:
            self.context.node_outputs[action_id] = outputs
            self.context.runtime_state["last_outputs"] = outputs

    def merge_context_partitions(self, context: ActionContext) -> None:
        """
        合并动作返回的新分区上下文。
        """
        for key in ("workflow_context", "runtime_state", "artifacts"):
            value = getattr(context, key, None)
            if not value:
                continue
            current_value = getattr(self.context, key, None) or {}
            current_value.update(value)
            setattr(self.context, key, current_value)

    def merge_action_outputs(self, action: Action, outputs: dict) -> None:
        """
        按声明式合并策略写入全局上下文和 artifacts 分区。
        """
        for key, value in outputs.items():
            if value in (None, "", [], {}):
                continue
            output_config = self.get_action_output_config(action, key)
            target_key = output_config.get("target") or key
            merge_policy = output_config.get("merge") or self.get_default_merge_policy(action, target_key, value)
            identity = output_config.get("identity")
            self.merge_output_value(target_key, value, merge_policy, identity)

    def merge_output_value(self, key: str, value: Any, merge_policy: str, identity: Optional[str] = None) -> None:
        """
        按指定策略合并单个输出值。
        """
        current_value = getattr(self.context, key, None) if key in ActionContext.model_fields else None
        merged_value = self.apply_merge_policy(current_value, value, merge_policy, identity)
        if key in ActionContext.model_fields:
            setattr(self.context, key, merged_value)
        if key in ARTIFACT_FIELDS:
            current_artifact = self.context.artifacts.get(key)
            self.context.artifacts[key] = self.apply_merge_policy(current_artifact, value, merge_policy, identity)

    def get_action_output_config(self, action: Action, output_key: str) -> dict:
        """
        获取动作输出声明配置。
        """
        outputs_config = action.outputs or self.get_action_data_value(action, "outputs") or {}
        if isinstance(outputs_config, dict):
            value = outputs_config.get(output_key) or {}
            return value if isinstance(value, dict) else {}
        return {}

    def get_default_merge_policy(self, action: Action, key: str, value: Any) -> str:
        """
        获取输出默认合并策略。
        """
        if action.type in ("FilterTorrentsAction", "FilterMediasAction", "FetchDownloadsAction"):
            return "replace"
        if isinstance(value, list):
            return "append_unique"
        if isinstance(value, dict):
            return "merge_dict"
        return "first_non_empty"

    def apply_merge_policy(self, current_value: Any, value: Any, merge_policy: str,
                           identity: Optional[str] = None) -> Any:
        """
        应用声明式合并策略。
        """
        if merge_policy == "replace":
            return value
        if merge_policy == "merge_dict":
            merged = current_value.copy() if isinstance(current_value, dict) else {}
            if isinstance(value, dict):
                merged.update(value)
                return merged
            return current_value or value
        if merge_policy == "append_unique":
            return self.append_unique_values(current_value, value, identity)
        if merge_policy == "first_non_empty":
            return current_value or value
        return current_value or value

    def append_unique_values(self, current_value: Any, value: Any, identity: Optional[str] = None) -> list:
        """
        追加列表并按身份字段去重。
        """
        current_list = list(current_value or [])
        incoming_list = value if isinstance(value, list) else [value]
        seen = {self.get_identity_value(item, identity) for item in current_list}
        for item in incoming_list:
            identity_value = self.get_identity_value(item, identity)
            if identity_value in seen:
                continue
            current_list.append(item)
            seen.add(identity_value)
        return current_list

    def get_identity_value(self, item: Any, identity: Optional[str] = None) -> Any:
        """
        获取列表元素去重身份。
        """
        if not identity:
            identity_value = self.make_json_safe(item)
            return self.make_hashable_identity(identity_value)
        value = item
        for part in identity.split("."):
            value = self.read_value(value, int(part) if part.isdigit() else part)
        return self.make_hashable_identity(self.make_json_safe(value))

    @staticmethod
    def make_hashable_identity(value: Any) -> Any:
        """
        将身份值转换为可哈希对象。
        """
        try:
            hash(value)
            return value
        except TypeError:
            return repr(value)

    def call_step_callback(self, action: Action, completed: bool) -> None:
        """
        持久化当前步骤上下文和结构化执行状态。
        """
        if not self.step_callback:
            return
        callback_params = inspect.signature(self.step_callback).parameters
        if len(callback_params) <= 2:
            self.step_callback(action, self.context)
            return
        self.step_callback(action, self.context, self.build_execution_state(), completed)

    @staticmethod
    def extract_context_outputs(context: ActionContext) -> dict:
        """
        从动作上下文中提取非空业务字段作为节点默认输出。
        """
        if not context:
            return {}
        outputs = {}
        for key in context.__class__.model_fields:
            if key in ("execute_history", "progress", "node_outputs", "runtime_state"):
                continue
            value = getattr(context, key, None)
            if value in (None, "", [], {}):
                continue
            outputs[key] = value
        return outputs

    @staticmethod
    def get_flow_key(flow: ActionFlow) -> str:
        """
        生成流程边的运行期唯一标识。
        """
        return flow.id or f"{flow.source}->{flow.target}:{id(flow)}"

    def get_action_join_policy(self, action: Action, incoming_flows: List[ActionFlow]) -> str:
        """
        获取动作汇合策略，优先使用动作配置，其次兼容流程边配置。
        """
        join_policy = action.join_policy or self.get_action_data_value(action, "join_policy")
        if join_policy:
            return join_policy
        for flow in incoming_flows:
            join_policy = flow.join_policy or self.get_flow_data_value(flow, "join_policy")
            if join_policy:
                return join_policy
        return "all_success"

    def get_action_branch_policy(self, action: Optional[Action], outgoing_flows: List[ActionFlow]) -> str:
        """
        获取动作出边分支策略。
        """
        if action:
            branch_policy = action.branch_policy or self.get_action_data_value(action, "branch_policy")
            if branch_policy:
                return branch_policy
        for flow in outgoing_flows:
            branch_policy = flow.branch_policy or self.get_flow_data_value(flow, "branch_policy")
            if branch_policy:
                return branch_policy
        return "parallel"

    def get_action_fail_policy(self, action: Action) -> str:
        """
        获取动作失败策略。
        """
        return action.fail_policy or self.get_action_data_value(action, "fail_policy") or "stop"

    def get_action_concurrency_key(self, action: Action) -> Optional[str]:
        """
        获取动作并发互斥键。
        """
        return action.concurrency_key or self.get_action_data_value(action, "concurrency_key")

    def get_flow_condition(self, flow: ActionFlow) -> Optional[str]:
        """
        获取流程边条件表达式。
        """
        return flow.condition or self.get_flow_data_value(flow, "condition")

    @staticmethod
    def get_action_data_value(action: Optional[Action], key: str) -> Any:
        """
        从动作 data 中读取扩展配置。
        """
        if not action:
            return None
        data = action.data or {}
        return data.get(key) if isinstance(data, dict) else None

    @staticmethod
    def get_flow_data_value(flow: ActionFlow, key: str) -> Any:
        """
        从流程边 data 中读取扩展配置。
        """
        data = flow.data or {}
        return data.get(key) if isinstance(data, dict) else None

    def evaluate_condition(self, condition: Optional[str]) -> bool:
        """
        安全计算流程边条件表达式。
        """
        if not condition:
            return True
        expression = condition.strip()
        if not expression:
            return True
        expression = expression.replace("&&", " and ").replace("||", " or ")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as err:
            raise ValueError(f"{condition} 语法错误") from err
        return bool(self.evaluate_condition_node(tree.body))

    def evaluate_condition_node(self, node: ast.AST) -> Any:
        """
        递归计算受限 AST 节点，避免执行任意代码。
        """
        if isinstance(node, ast.BoolOp):
            values = [bool(self.evaluate_condition_node(value)) for value in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self.evaluate_condition_node(node.operand))
        if isinstance(node, ast.Compare):
            return self.evaluate_compare_node(node)
        if isinstance(node, ast.Name):
            return self.resolve_condition_name(node.id)
        if isinstance(node, ast.Attribute):
            return self.read_value(self.evaluate_condition_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            return self.read_subscript_node(node)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.List):
            return [self.evaluate_condition_node(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.evaluate_condition_node(item) for item in node.elts)
        if isinstance(node, ast.Set):
            return {self.evaluate_condition_node(item) for item in node.elts}
        if isinstance(node, ast.Dict):
            return {
                self.evaluate_condition_node(key): self.evaluate_condition_node(value)
                for key, value in zip(node.keys, node.values)
            }
        raise ValueError(f"不支持的条件表达式：{ast.dump(node)}")

    def evaluate_compare_node(self, node: ast.Compare) -> bool:
        """
        计算比较表达式，支持链式比较和成员判断。
        """
        left = self.evaluate_condition_node(node.left)
        for operator, comparator in zip(node.ops, node.comparators):
            right = self.evaluate_condition_node(comparator)
            if not self.compare_values(left, operator, right):
                return False
            left = right
        return True

    def read_subscript_node(self, node: ast.Subscript) -> Any:
        """
        读取下标访问表达式。
        """
        if isinstance(node.slice, ast.Slice):
            raise ValueError("条件表达式不支持切片访问")
        container = self.evaluate_condition_node(node.value)
        key = self.evaluate_condition_node(node.slice)
        return self.read_value(container, key)

    def resolve_condition_name(self, name: str) -> Any:
        """
        将条件表达式中的根名称映射到当前工作流上下文。
        """
        if name in ("true", "True"):
            return True
        if name in ("false", "False"):
            return False
        if name in ("none", "None", "null"):
            return None
        if name == "context":
            return self.context
        if name == "workflow_context":
            return self.context.workflow_context or {}
        if name == "runtime_state":
            return self.context.runtime_state or {}
        if name == "artifacts":
            return self.context.artifacts or {}
        if name in ("outputs", "node_outputs"):
            return self.context.node_outputs or {}
        if name == "last":
            return self.context.runtime_state.get("last_outputs") if self.context.runtime_state else {}
        if name in (self.context.node_outputs or {}):
            return self.context.node_outputs[name]
        if name in ActionContext.model_fields:
            return getattr(self.context, name, None)
        raise ValueError(f"未知上下文变量 {name}")

    def resolve_context_path(self, path: str) -> Any:
        """
        按点分路径读取工作流上下文数据。
        """
        if not path:
            return None
        value = None
        for index, part in enumerate(path.split(".")):
            if index == 0:
                value = self.resolve_condition_name(part)
                continue
            key = int(part) if part.isdigit() else part
            value = self.read_value(value, key)
        return value

    @staticmethod
    def read_value(value: Any, key: Any) -> Any:
        """
        从 dict、对象或序列中读取属性值。
        """
        if value is None:
            return None
        if isinstance(key, str) and key in ("count", "length") and hasattr(value, "__len__"):
            return len(value)
        if isinstance(value, dict):
            return value.get(key)
        if isinstance(value, (list, tuple)):
            if isinstance(key, int) and 0 <= key < len(value):
                return value[key]
            return None
        if isinstance(key, str) and hasattr(value, key):
            return getattr(value, key)
        return None

    @staticmethod
    def compare_values(left: Any, operator: ast.cmpop, right: Any) -> bool:
        """
        比较两个条件表达式值。
        """
        try:
            if isinstance(operator, ast.Eq):
                return left == right
            if isinstance(operator, ast.NotEq):
                return left != right
            if isinstance(operator, ast.Gt):
                return left > right
            if isinstance(operator, ast.GtE):
                return left >= right
            if isinstance(operator, ast.Lt):
                return left < right
            if isinstance(operator, ast.LtE):
                return left <= right
            if isinstance(operator, ast.In):
                return left in right
            if isinstance(operator, ast.NotIn):
                return left not in right
        except TypeError:
            return False
        raise ValueError(f"不支持的比较操作符：{operator.__class__.__name__}")

    def merge_context(self, context: ActionContext) -> None:
        """
        合并上下文
        """
        if not context:
            return
        for key in context.__class__.model_fields:
            value = getattr(context, key, None)
            if key in ("execute_history", "progress") or value in (None, "", [], {}):
                continue
            current_value = getattr(self.context, key, None)
            if isinstance(value, list):
                if current_value is None:
                    setattr(self.context, key, value)
                    continue
                for item in value:
                    if item not in current_value:
                        current_value.append(item)
            elif isinstance(value, dict):
                if not current_value:
                    setattr(self.context, key, value)
                else:
                    current_value.update(value)
            elif not current_value:
                setattr(self.context, key, value)


class WorkflowChain(ChainBase):
    """
    工作流链
    """

    @eventmanager.register(EventType.WorkflowExecute)
    def event_process(self, event: Event):
        """
        事件触发工作流执行
        """
        workflow_id = event.event_data.get('workflow_id')
        if not workflow_id:
            return
        self.process(workflow_id, from_begin=False)

    @staticmethod
    def process(workflow_id: int, from_begin: Optional[bool] = True) -> Tuple[bool, str]:
        """
        处理工作流
        :param workflow_id: 工作流ID
        :param from_begin: 是否从头开始，默认为True
        """
        workflowoper = WorkflowOper()

        def save_step(action: Action, context: ActionContext, execution_state: dict, completed: bool):
            """
            保存上下文到数据库
            """
            # 序列化数据
            serialized_data = pickle.dumps(context)
            # 使用Base64编码字节流
            encoded_data = base64.b64encode(serialized_data).decode('utf-8')
            WorkflowOper().step(
                workflow_id,
                action_id=action.id if completed else "",
                context={
                    "content": encoded_data
                },
                execution_state=execution_state
            )

        # 重置工作流
        if from_begin:
            workflowoper.reset(workflow_id)

        # 查询工作流数据
        workflow = workflowoper.get(workflow_id)
        if not workflow:
            logger.warn(f"工作流 {workflow_id} 不存在")
            return False, "工作流不存在"
        if not workflow.actions:
            logger.warn(f"工作流 {workflow.name} 无动作")
            return False, "工作流无动作"
        if not workflow.flows:
            logger.warn(f"工作流 {workflow.name} 无流程")
            return False, "工作流无流程"

        logger.info(f"开始执行工作流 {workflow.name}，共 {len(workflow.actions)} 个动作 ...")
        workflowoper.start(workflow_id)

        # 执行工作流
        executor = WorkflowExecutor(workflow, step_callback=save_step)
        executor.execute()

        if executor.stopped:
            logger.info(f"工作流 {workflow.name} 已停止")
            return False, executor.errmsg

        if not executor.success or executor.has_failure:
            logger.info(f"工作流 {workflow.name} 执行失败：{executor.errmsg}")
            workflowoper.fail(workflow_id, result=executor.errmsg)
            return False, executor.errmsg
        logger.info(f"工作流 {workflow.name} 执行完成")
        workflowoper.success(workflow_id)
        return True, ""

    @staticmethod
    def get_workflows() -> List[Workflow]:
        """
        获取工作流列表
        """
        return WorkflowOper().list_enabled()

    @staticmethod
    def get_timer_workflows() -> List[Workflow]:
        """
        获取定时触发的工作流列表
        """
        return WorkflowOper().get_timer_triggered_workflows()

    @staticmethod
    def get_event_workflows() -> List[Workflow]:
        """
        获取事件触发的工作流列表
        """
        return WorkflowOper().get_event_triggered_workflows()
