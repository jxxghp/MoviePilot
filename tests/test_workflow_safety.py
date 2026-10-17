"""
工作流准入、真实线程生命周期、截止时间和图定义的行为回归。
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import workflow as workflow_package
from app.application.workflow import WorkflowDefinitionCommand, WorkflowMutationCommand
from app.chain import workflow as workflow_chain
from app.schemas.workflow import Action, ActionResult


def _workflow(workflow_id=1, *, actions=None, flows=None, current_action=None):
    """
    构造包含串行两个动作的脱离数据库工作流快照。
    """
    return SimpleNamespace(
        id=workflow_id, name=f"workflow-{workflow_id}", current_action=current_action,
        actions=actions if actions is not None else [
            {"id": "A", "name": "A", "type": "ObservedAction"},
            {"id": "B", "name": "B", "type": "ObservedAction"},
        ],
        flows=flows if flows is not None else [{"id": "AB", "source": "A", "target": "B"}],
        context={}, execution_config={}, execution_state={},
    )


def _manager(action_type):
    """
    提供真实准入锁和动作调度能力，不启动应用或加载外部插件。
    """
    manager = object.__new__(workflow_package.WorkflowManager)
    manager._lock = threading.RLock()
    manager._actions = {"ObservedAction": action_type}
    manager._event_workflows = {}
    manager._accepting_executions = True
    manager._executions = {}
    return manager


def _install_runtime(monkeypatch, manager, workflows):
    """
    把真实执行链绑定到可观察的内存查询和事务端口。
    """
    port = SimpleNamespace(reset=Mock(return_value=True), start=Mock(return_value=True),
                           step=Mock(return_value=True), success=Mock(return_value=True), fail=Mock(return_value=True))
    monkeypatch.setattr(workflow_chain, "get_workflow_manager", lambda: manager)
    monkeypatch.setattr(workflow_chain, "get_configured_workflow_execution", lambda: port)
    monkeypatch.setattr(workflow_chain, "get_configured_workflow_query",
                        lambda: SimpleNamespace(get_sync=workflows.get))
    monkeypatch.setattr(workflow_package.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)
    return port


def test_same_workflow_from_begin_cannot_reset_active_execution(monkeypatch):
    """
    真实节点线程阻塞时，同 ID 重跑必须在重置和写运行中状态之前被拒绝。
    """
    entered, release = threading.Event(), threading.Event()
    calls = []

    class ObservedAction:
        """
        记录动作并把第一个节点阻塞到竞争请求结束。
        """

        def __init__(self, action_id):
            """
            保存当前节点 ID。
            """
            self.action_id = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            在真实池线程内阻塞节点 A，其他节点直接成功。
            """
            _ = workflow_id, params, inputs, runtime
            calls.append(self.action_id)
            if self.action_id == "A":
                entered.set()
                assert release.wait(2)
            return ActionResult(success=True, message="", context=context)

    manager = _manager(ObservedAction)
    port = _install_runtime(monkeypatch, manager, {1: _workflow(current_action="A,B")})
    results = []
    thread = threading.Thread(target=lambda: results.append(workflow_chain.WorkflowChain.process(1)), daemon=True)
    thread.start()
    try:
        assert entered.wait(1), "from_begin 应清除旧节点状态并真正执行 A"
        refused = workflow_chain.WorkflowChain.process(1, from_begin=True)
        assert refused[0] is False
        port.reset.assert_called_once_with(1)
        port.start.assert_called_once_with(1)
        assert calls == ["A"]
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert results == [(True, "")]
    assert calls == ["A", "B"]
    assert manager._executions == {}


@pytest.mark.parametrize("succeeded", [True, False])
def test_workflow_retains_owner_until_terminal_transaction_commits(monkeypatch, succeeded):
    """
    成功和失败终态提交阻塞时均保留同 ID 所有权，防止覆盖后续运行。
    """
    entered, release = threading.Event(), threading.Event()

    class ObservedAction:
        """
        以预定结果结束动作，令执行进入终态提交。
        """

        def __init__(self, action_id):
            """
            接收动作标识并保持无副作用。
            """
            _ = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            直接结束，成功和失败走各自真实写入分支。
            """
            _ = workflow_id, params, inputs, runtime
            return ActionResult(success=succeeded, message="failed", context=context)

    manager = _manager(ObservedAction)
    port = _install_runtime(monkeypatch, manager, {1: _workflow()})

    def persist_terminal(*_args, **_kwargs):
        """
        让终态事务在尚未完成提交时保持阻塞。
        """
        entered.set()
        assert release.wait(2)
        return True

    terminal = port.success if succeeded else port.fail
    terminal.side_effect = persist_terminal
    results = []
    thread = threading.Thread(target=lambda: results.append(workflow_chain.WorkflowChain.process(1)), daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        assert manager._executions
        assert workflow_chain.WorkflowChain.process(1)[0] is False
        owner = next(iter(manager._executions.values()))
        assert owner.wait_stopped(timeout=0.01) is False
        port.reset.assert_called_once()
        port.start.assert_called_once()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert results[0][0] is succeeded
    assert manager._executions == {}


def test_different_workflows_can_execute_concurrently(monkeypatch):
    """
    同 ID 互斥不应把不同工作流串行化。
    """
    entered = {1: threading.Event(), 2: threading.Event()}
    release = threading.Event()

    class ObservedAction:
        """
        在两个工作流的首节点上记录并发进入。
        """

        def __init__(self, action_id):
            """
            保存节点 ID。
            """
            self.action_id = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            首节点保持阻塞，以证明两个不同 ID 同时被准入。
            """
            _ = params, inputs, runtime
            if self.action_id == "A":
                entered[workflow_id].set()
                assert release.wait(2)
            return ActionResult(success=True, message="", context=context)

    manager = _manager(ObservedAction)
    _install_runtime(monkeypatch, manager, {wid: _workflow(wid) for wid in entered})
    results = []
    threads = [threading.Thread(target=lambda wid=wid: results.append(workflow_chain.WorkflowChain.process(wid)),
                                daemon=True) for wid in entered]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(1) for event in entered.values())
        assert len(manager._executions) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
    assert results == [(True, ""), (True, "")]


@pytest.mark.parametrize("rejected_operation", ["reset", "start"])
def test_failed_start_transaction_releases_unstarted_owner(monkeypatch, rejected_operation):
    """
    重置或启动事务返回 False 时，不得执行节点或提交成功终态。
    """
    manager = _manager(Mock())
    port = _install_runtime(monkeypatch, manager, {1: _workflow()})
    getattr(port, rejected_operation).return_value = False
    success, message = workflow_chain.WorkflowChain.process(1)
    assert success is False
    assert "失败" in message
    assert manager._executions == {}
    manager._actions["ObservedAction"].assert_not_called()
    port.success.assert_not_called()
    port.fail.assert_not_called()
    if rejected_operation == "reset":
        port.start.assert_not_called()


@pytest.mark.parametrize("commit_failure", [False, RuntimeError("terminal failed")])
def test_terminal_commit_failure_retains_owner_for_retry(monkeypatch, commit_failure):
    """
    终态未提交时不能报告成功或释放 owner，收口重试成功后才允许新执行。
    """
    action_type = Mock()
    action_type.get_contract.return_value = {}
    action_type.return_value.execute_with_inputs.return_value = ActionResult(success=True, message="")
    manager = _manager(action_type)
    port = _install_runtime(monkeypatch, manager, {1: _workflow()})
    port.success.side_effect = [commit_failure, True]
    with pytest.raises(RuntimeError):
        workflow_chain.WorkflowChain.process(1)
    assert manager._executions
    assert workflow_chain.WorkflowChain.process(1)[0] is False
    port.start.assert_called_once()
    owner = next(iter(manager._executions.values()))
    assert owner.wait_stopped(timeout=1) is True
    assert port.success.call_count == 2
    assert manager._executions == {}


@pytest.mark.parametrize("loop", [False, True])
def test_action_timeout_checks_result_and_deadline_after_sleep(monkeypatch, loop):
    """
    同步返回和循环等待跨过截止时间时均失败，等待后不能再次调用动作。
    """
    clock = [0.0]
    calls, sleeps = [], []

    class ObservedAction:
        """
        用离线时钟模拟同步阻塞或未完成的循环动作。
        """

        done = False

        def __init__(self, action_id):
            """
            保留构造兼容，不引入外部能力。
            """
            _ = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            非循环跨过截止时间，循环首轮保持未完成。
            """
            _ = workflow_id, params, inputs, runtime
            calls.append("execute")
            if not loop:
                clock[0] = 2
            return ActionResult(success=True, message="", context=context)

    def simulated_sleep(_wid, seconds, _token):
        """
        推进测试时钟并记录调度器裁剪后的等待时间。
        """
        sleeps.append(seconds)
        clock[0] += seconds

    manager = _manager(ObservedAction)
    monkeypatch.setattr(workflow_package, "monotonic", lambda: clock[0])
    monkeypatch.setattr(manager, "_sleep_with_cancel", simulated_sleep)
    monkeypatch.setattr(workflow_package.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)
    result = manager.execute(1, Action(id="A", type="ObservedAction", timeout=1,
                                     data={"loop": loop, "loop_interval": 10}))
    assert result.success is False
    assert "超时" in result.message
    assert calls == ["execute"]
    assert sleeps == ([1] if loop else [])


def test_cooperative_action_can_observe_deadline_cancel_token(monkeypatch):
    """
    支持取消的动作通过运行期令牌看到本次尝试的截止时间。
    """
    clock = [0.0]

    class ObservedAction:
        """
        在动作内部协作检查截止时间，而非让管理器分离后台线程。
        """

        def __init__(self, action_id):
            """
            接收动作标识。
            """
            _ = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            直到统一令牌报告取消才退出，并验证传入截止时间。
            """
            _ = workflow_id, params, inputs
            token = runtime["cancel_token"]
            assert runtime["deadline"] == 1
            while not token.is_cancelled():
                clock[0] += 0.25
            return ActionResult(success=False, message="cancelled", context=context)

    manager = _manager(ObservedAction)
    monkeypatch.setattr(workflow_package, "monotonic", lambda: clock[0])
    monkeypatch.setattr(workflow_package.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)
    result = manager.execute(1, Action(id="A", type="ObservedAction", timeout=1))
    assert result.success is False
    assert "超时" in result.message
    assert clock[0] == 1


def test_noncooperative_timeout_keeps_owner_until_thread_really_returns(monkeypatch):
    """
    到达截止时间也不能分离阻塞线程或提前放行同 ID 的下一次执行。
    """
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    clock = [0.0]

    class ObservedAction:
        """
        模拟不读取协作令牌的第三方同步动作。
        """

        def __init__(self, action_id):
            """
            接收动作标识。
            """
            _ = action_id

        def execute_with_inputs(self, workflow_id, params, inputs, runtime, context):
            """
            忽略令牌，直到测试显式释放才真实返回。
            """
            _ = workflow_id, params, inputs, runtime
            entered.set()
            assert release.wait(2)
            return ActionResult(success=True, message="", context=context)

    manager = _manager(ObservedAction)
    definition = _workflow(actions=[{"id": "A", "name": "A", "type": "ObservedAction", "timeout": 1}], flows=[])
    port = _install_runtime(monkeypatch, manager, {1: definition})
    monkeypatch.setattr(workflow_package, "monotonic", lambda: clock[0])
    results = []

    def run_workflow():
        """
        在独立宿主线程内运行，只有完整 process 返回才设置完成标志。
        """
        try:
            results.append(workflow_chain.WorkflowChain.process(1))
        finally:
            finished.set()

    thread = threading.Thread(target=run_workflow, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        clock[0] = 2
        assert finished.wait(0.05) is False
        assert manager._executions
        assert workflow_chain.WorkflowChain.process(1)[0] is False
        port.reset.assert_called_once()
        port.fail.assert_not_called()
        port.success.assert_not_called()
    finally:
        release.set()
        thread.join(2)
    assert results[0][0] is False
    assert "超时" in port.step.call_args.kwargs["execution_state"]["nodes"]["A"]["message"]
    port.fail.assert_called_once()
    assert manager._executions == {}


_INVALID_GRAPHS = [
    ([{"id": "A"}, {"id": "A"}], [], "重复"),
    ([{"id": "A"}], [{"source": "A", "target": "missing"}], "不存在"),
    ([{"id": "A"}], [{"source": "A", "target": ""}], "端点"),
    ([{"id": "A"}, {"id": "B"}],
     [{"source": "A", "target": "B"}, {"source": "B", "target": "A"}], "循环"),
    ([{"id": "A"}, {"id": "B"}, {"id": "C"}],
     [{"source": "B", "target": "C"}, {"source": "C", "target": "B"}], "循环"),
    ([{"id": "A"}, {"id": "B"}],
     [{"id": "same", "source": "A", "target": "B"}, {"id": "same", "source": "A", "target": "B"}], "重复"),
]


@pytest.mark.parametrize("actions,flows,error", _INVALID_GRAPHS)
def test_persisted_invalid_graph_cannot_report_runtime_success(monkeypatch, actions, flows, error):
    """
    即使坏定义已持久化，运行入口也应拒绝且不写成功或启动状态。
    """
    manager = _manager(object)
    port = _install_runtime(monkeypatch, manager, {1: _workflow(actions=actions, flows=flows)})
    success, message = workflow_chain.WorkflowChain.process(1)
    assert success is False
    assert error in message
    port.start.assert_not_called()
    port.success.assert_not_called()


@pytest.mark.parametrize("actions,flows,error", _INVALID_GRAPHS)
@pytest.mark.parametrize("operation", ["create", "update", "fork"])
def test_invalid_graph_is_rejected_before_definition_write(actions, flows, error, operation):
    """
    创建、更新和复用共享同一图校验，错误不触发任何数据库写入。
    """
    repository = SimpleNamespace(async_get_by_name=AsyncMock(return_value=None),
                                 stage_create=AsyncMock(), stage_update=Mock(),
                                 get=Mock(return_value=SimpleNamespace(trigger_type="manual", actions=[], flows=[])))
    if operation == "update":
        unit = SimpleNamespace(commit=Mock(), rollback=Mock())
        command = WorkflowMutationCommand(
            repository=repository, unit_of_work=unit, add_timer=Mock(), remove_timer=Mock(),
            load_event=Mock(), remove_event=Mock(), refresh_event=Mock(), stop_running=Mock(), delete_cache=Mock(),
        )
        result = command.update({"id": 1, "actions": actions, "flows": flows})
        repository.stage_update.assert_not_called()
        unit.commit.assert_not_called()
    else:
        unit = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        command = WorkflowDefinitionCommand(repository=repository, unit_of_work=unit,
                                            stop_running=Mock(), async_delete_cache=AsyncMock())
        payload = {"name": "invalid", "actions": actions, "flows": flows}
        if operation == "fork":
            payload.update(actions=json.dumps(actions), flows=json.dumps(flows))
        result = asyncio.run(getattr(command, operation)(payload))
        repository.stage_create.assert_not_awaited()
        unit.commit.assert_not_awaited()
    assert result.success is False
    assert error in result.message
