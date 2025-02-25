from datetime import datetime
from typing import List, Tuple

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.chain import ChainBase
from app.core.config import global_vars
from app.core.workflow import WorkFlowManager
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.schemas import Workflow, ActionContext, Action, ActionFlow


class WorkflowChain(ChainBase):
    """
    工作流链
    """

    def __init__(self):
        super().__init__()
        self.workflowoper = WorkflowOper()
        self.workflowmanager = WorkFlowManager()

    def process(self, workflow_id: int, from_begin: bool = True) -> Tuple[bool, str]:
        """
        处理工作流
        :param workflow_id: 工作流ID
        :param from_begin: 是否从头开始，默认为True
        """

        _init_action = None

        def __get_next_action(_workflow: Workflow, _action: str) -> List[Action]:
            """
            获取下一个动作
            """
            if not _action:
                # 获取起点动作
                actions = []
                source = [f.source for f in _workflow.flows]
                target = [f.target for f in _workflow.flows]
                for act in _workflow.actions:
                    if act.id not in target and act.id in source:
                        actions.append(Action(**act))
                return actions
            else:
                if _action == _init_action:
                    # 返回当前动作
                    action_ids = _action.split(',')
                    return [Action(**act) for act in _workflow.actions if act.id in action_ids]
                else:
                    # 获取下一个动作
                    flows = [ActionFlow(**f) for f in _workflow.flows if f.source == _action]
                    return [Action(**act) for act in _workflow.actions if act.id in [f.target for f in flows]]

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

        # 启用上下文
        if not from_begin and workflow.current_action:
            _init_action = workflow.current_action
            context = ActionContext(**workflow.context)
        else:
            context = ActionContext()

        if from_begin:
            current_action = None
        else:
            current_action = _init_action

        # 循环执行
        while next_actions := __get_next_action(workflow, current_action):
            if global_vars.is_system_stopped:
                break
            if not next_actions:
                break
            # 获取下一个动作
            if len(next_actions) > 1:
                # 多个下一步动作
                current_action = ",".join([act.id for act in next_actions])
                # 动作名称
                current_acttion_names = "、".join([act.name for act in next_actions])
                # 开始计时
                start_time = datetime.now()
                # 多个下一步动作，多线程并发执行，等待结果
                executor = ThreadPoolExecutor(max_workers=len(next_actions))
                all_task = []
                for action in next_actions:
                    task = executor.submit(self.workflowmanager.excute, action, context)
                    all_task.append(task)
                # 等待结果
                success_count = 0
                for future in as_completed(all_task):
                    state, context = future.result()
                    if state:
                        success_count += 1
                # 计算耗时
                end_time = datetime.now()
                # 记录步骤
                self.workflowoper.step(workflow_id,
                                       action=current_action,
                                       context=context.dict())
                if success_count < len(next_actions):
                    logger.error(f"动作 {current_acttion_names} 未全部成功，工作流失败")
                    self.workflowoper.fail(workflow_id, result=f"动作 {current_acttion_names} 未全部成功")
                    return False, f"动作 {current_acttion_names} 未全部成功"
                else:
                    logger.info(f"动作 {current_acttion_names} 执行完成，耗时：{(end_time - start_time).seconds} 秒")
            else:
                # 单个下一步动作
                action = next_actions[0]
                current_action = action.id
                # 开始计时
                start_time = datetime.now()
                # 执行动作
                state, context = self.workflowmanager.excute(action, context)
                # 计算耗时
                end_time = datetime.now()
                # 记录步骤
                self.workflowoper.step(workflow_id, action=current_action, context=context.dict())
                if not state:
                    logger.error(f"动作 {action.name} 执行失败，工作流失败")
                    self.workflowoper.fail(workflow_id, result=f"动作 {action.name} 执行失败")
                    return False, f"动作 {action.name} 执行失败"
                logger.info(f"动作 {action.name} 执行完成，耗时：{(end_time - start_time).seconds} 秒")

        logger.info(f"工作流 {workflow.name} 执行完成")
        self.workflowoper.success(workflow_id)
        return True, ""

    def get_workflows(self) -> List[Workflow]:
        """
        获取工作流列表
        """
        return self.workflowoper.list_enabled()
