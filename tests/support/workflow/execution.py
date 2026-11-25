"""
工作流管理器、节点与执行结果替身；不注册测试用例。
"""

from types import SimpleNamespace

from app.schemas.workflow import ActionContext, ActionResult


def _build_workflow(current_action=None, context=None, actions=None, flows=None,
                    execution_config=None, execution_state=None):
    """
    构造最小工作流对象。
    """
    return SimpleNamespace(
        id=1,
        name="测试工作流",
        actions=actions if actions is not None else [
            {"id": "A", "type": "FakeAction", "name": "动作A", "data": {}},
            {"id": "B", "type": "FakeAction", "name": "动作B", "data": {}},
        ],
        flows=flows if flows is not None else [
            {"id": "flow-1", "source": "A", "target": "B", "animated": True},
        ],
        current_action=current_action,
        context=context,
        execution_config=execution_config or {},
        execution_state=execution_state or {},
    )


class _FakeWorkflowManager:
    """
    记录执行动作的工作流管理器。
    """

    def __init__(self, calls, results=None, contracts=None):
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param calls: 用于断言操作顺序的记录列表
        :param results: 按节点索引的预设执行结果
        :param contracts: 按节点索引的输入输出合同
        """
        self.calls = calls
        self.results = results or {}
        self.contracts = contracts or {}
        self.received_inputs = []

    def execute(self, workflow_id, action, context=None, inputs=None, runtime=None, cancel_token=None):
        """
        执行伪动作并记录新版输入。
        """
        self.calls.append(action.id)
        self.received_inputs.append((action.id, inputs or {}, runtime or {}, cancel_token))
        result = self.results.get(action.id)
        if callable(result):
            return result(action, context or ActionContext())
        if result:
            return result
        return ActionResult(success=True, message=f"{action.name}完成", context=context or ActionContext())

    def excute(self, workflow_id, action, context=None):
        """
        兼容历史执行方法。
        """
        result = self.execute(workflow_id, action, context)
        return result.success, result.message, result.context

    def get_action_contract(self, action_type):
        """
        获取伪动作契约。
        """
        return self.contracts.get(action_type) or {}
