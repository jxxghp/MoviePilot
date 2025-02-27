import base64
import pickle
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from typing import List, Tuple

from pydantic.fields import Callable

from app.chain import ChainBase
from app.core.config import global_vars
from app.core.workflow import WorkFlowManager
from app.db.models import Workflow
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.schemas import ActionContext, ActionFlow, Action


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

        self.success = True
        self.errmsg = ""

        # 工作流管理器
        self.workflowmanager = WorkFlowManager()
        # 线程安全队列
        self.queue = deque()
        # 锁用于保证线程安全
        self.lock = threading.Lock()
        # 线程池
        self.executor = ThreadPoolExecutor()
        # 跟踪运行中的任务数
        self.running_tasks = 0

        # 构建邻接表、入度表
        self.adjacency = defaultdict(list)
        self.indegree = defaultdict(int)
        for flow in self.flows:
            source = flow.source
            target = flow.target
            self.adjacency[source].append(target)
            self.indegree[target] += 1

        # 初始化所有节点的入度（确保未被引用的节点入度为0）
        for action_id in self.actions:
            if action_id not in self.indegree:
                self.indegree[action_id] = 0

        # 初始上下文
        if workflow.current_action and workflow.context:
            # Base64解码
            decoded_data = base64.b64decode(workflow.context["content"])
            # 反序列化数据
            self.context = pickle.loads(decoded_data)
        else:
            self.context = ActionContext()

        # 初始化队列：入度为0的节点
        for action_id in self.actions:
            if self.indegree[action_id] == 0:
                self.queue.append(action_id)

    def execute(self):
        """
        执行工作流
        """
        while True:
            with self.lock:
                # 退出条件：队列为空且无运行任务
                if not self.queue and self.running_tasks == 0:
                    break
                # 退出条件：出现了错误
                if not self.success:
                    break
                if not self.queue:
                    sleep(1)
                    continue
                # 取出队首节点
                node_id = self.queue.popleft()
                # 标记任务开始
                self.running_tasks += 1

            # 已停机
            if global_vars.is_system_stopped:
                break

            # 已执行的跳过
            if (self.workflow.current_action
                    and node_id in self.workflow.current_action.split(',')):
                continue

            # 提交任务到线程池
            future = self.executor.submit(
                self.execute_node,
                node_id,
                self.context
            )
            future.add_done_callback(self.on_node_complete)

    def execute_node(self, node_id: int, context: ActionContext) -> Tuple[Action, bool, ActionContext]:
        """
        执行单个节点操作，返回修改后的上下文和节点ID
        """
        action = self.actions[node_id]
        state, result_ctx = self.workflowmanager.excute(action, context=context)
        return action, state, result_ctx

    def on_node_complete(self, future):
        """
        节点完成回调：更新上下文、处理后继节点
        """
        action, state, result_ctx = future.result()

        # 节点执行失败
        if not state:
            self.success = False
            self.errmsg = f"{action.name} 失败"
            # 标记任务完成
            with self.lock:
                self.running_tasks -= 1

            return

        with self.lock:
            # 更新主上下文
            self.merge_context(result_ctx)
            # 回调
            if self.step_callback:
                self.step_callback(action, self.context)

        # 处理后继节点
        successors = self.adjacency.get(action.id, [])
        for succ_id in successors:
            with self.lock:
                self.indegree[succ_id] -= 1
                if self.indegree[succ_id] == 0:
                    self.queue.append(succ_id)

        # 标记任务完成
        with self.lock:
            self.running_tasks -= 1

    def merge_context(self, context: ActionContext):
        """
        合并上下文
        """
        for key, value in context.dict().items():
            if not getattr(self.context, key, None):
                setattr(self.context, key, value)


class WorkflowChain(ChainBase):
    """
    工作流链
    """

    def __init__(self):
        super().__init__()
        self.workflowoper = WorkflowOper()

    def process(self, workflow_id: int, from_begin: bool = True) -> Tuple[bool, str]:
        """
        处理工作流
        :param workflow_id: 工作流ID
        :param from_begin: 是否从头开始，默认为True
        """

        def save_step(action: Action, context: ActionContext):
            """
            保存上下文到数据库
            """
            # 序列化数据
            serialized_data = pickle.dumps(context)
            # 使用Base64编码字节流
            encoded_data = base64.b64encode(serialized_data).decode('utf-8')
            self.workflowoper.step(workflow_id, action_id=action.id, context={
                "content": encoded_data
            })

        # 重置工作流
        if from_begin:
            self.workflowoper.reset(workflow_id)

        # 查询工作流数据
        workflow = self.workflowoper.get(workflow_id)
        if not workflow:
            logger.warn(f"工作流 {workflow_id} 不存在")
            return False, "工作流不存在"
        if not workflow.actions:
            logger.warn(f"工作流 {workflow.name} 无动作")
            return False, "工作流无动作"
        if not workflow.flows:
            logger.warn(f"工作流 {workflow.name} 无流程")
            return False, "工作流无流程"

        logger.info(f"开始处理 {workflow.name}，共 {len(workflow.actions)} 个动作 ...")
        self.workflowoper.start(workflow_id)

        # 执行工作流
        executor = WorkflowExecutor(workflow, step_callback=save_step)
        executor.execute()

        if not executor.success:
            logger.info(f"工作流 {workflow.name} 执行失败：{executor.errmsg}！")
            self.workflowoper.fail(workflow_id, result=executor.errmsg)
            return False, executor.errmsg
        else:
            logger.info(f"工作流 {workflow.name} 执行成功")
            self.workflowoper.success(workflow_id)
            return True, ""

    def get_workflows(self) -> List[Workflow]:
        """
        获取工作流列表
        """
        return self.workflowoper.list_enabled()
