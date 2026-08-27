"""验证历史重试入口只把 durable 任务交给持久恢复调度器。"""

from types import SimpleNamespace

from app.application.transfer.execution import (
    TransferExecutionState,
    TransferRetryRequestResult,
)
from app.chain.transfer import TransferChain
from app.schemas.types import NotificationChannel


class _RetryCommand:
    """记录历史入口提交的类型化重试请求。"""

    calls: list[tuple[object, dict]] = []
    result = TransferRetryRequestResult(
        accepted=True,
        state=TransferExecutionState.RETRY_WAIT,
        retry_generation=2,
        message="整理任务已登记重试",
    )

    def __init__(self, repository: object) -> None:
        """保存测试仓储实例。"""
        self._repository = repository

    def request_retry(self, **kwargs) -> TransferRetryRequestResult:
        """记录请求并返回用例指定结果。"""
        self.calls.append((self._repository, kwargs))
        return self.result


def _install_retry_port(monkeypatch) -> object:
    """安装不会接触数据库的 execution 端口与命令替身。"""
    repository = object()
    _RetryCommand.calls = []
    _RetryCommand.result = TransferRetryRequestResult(
        accepted=True,
        state=TransferExecutionState.RETRY_WAIT,
        retry_generation=2,
        message="整理任务已登记重试",
    )
    monkeypatch.setattr(
        "app.chain._transfer.get_chain_transfer_execution_port",
        lambda: repository,
    )
    monkeypatch.setattr(
        "app.chain._transfer.TransferExecutionCommand",
        _RetryCommand,
    )
    return repository


def test_durable_history_redo_only_requests_persistent_retry(monkeypatch):
    """durable 重做不得检查源文件、重新识别或重新准入执行。"""
    repository = _install_retry_port(monkeypatch)
    history = SimpleNamespace(
        id=81,
        transfer_task_id="transfer-task-81",
        src="/missing/source.mkv",
    )
    monkeypatch.setattr(
        "app.chain._transfer.get_chain_transfer_history_port",
        lambda: SimpleNamespace(get=lambda history_id: history),
    )
    monkeypatch.setattr(
        "app.chain._transfer.Path.exists",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("durable 重试不应检查源路径")
        ),
    )
    chain = object.__new__(TransferChain)
    monkeypatch.setattr(
        chain,
        "do_transfer",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable 重试不应重新准入")
        ),
    )

    state, message = chain._re_transfer(logid=81)

    assert state is True
    assert message == "整理任务已登记重试"
    assert _RetryCommand.calls == [
        (
            repository,
            {
                "task_id": "transfer-task-81",
                "reason": "用户请求重试整理历史 #81",
                "requested_by": "history_redo",
            },
        )
    ]


def test_durable_manual_cleanup_keeps_target_history_and_failure_budget(monkeypatch):
    """手动重整命中 durable 历史时不得先删目标、历史或失败计数。"""
    _install_retry_port(monkeypatch)
    history = SimpleNamespace(
        id=82,
        transfer_task_id="transfer-task-82",
        status=False,
        mode="copy",
        src="/downloads/source.mkv",
        src_storage="local",
        dest_fileitem={
            "storage": "local",
            "path": "/library/source.mkv",
            "type": "file",
        },
    )
    history_port = SimpleNamespace(
        delete=lambda _history_id: (_ for _ in ()).throw(
            AssertionError("durable 历史不得删除")
        )
    )
    monkeypatch.setattr(
        "app.chain._transfer.StorageChain",
        lambda: (_ for _ in ()).throw(
            AssertionError("durable 目标不得删除")
        ),
    )
    monkeypatch.setattr(
        "app.chain._transfer.clear_transfer_failures",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("durable 失败计数不得清零")
        ),
    )
    chain = object.__new__(TransferChain)

    state, message = chain._delete_manual_transfer_history(
        history=history,
        transfer_history_oper=history_port,
    )

    assert state is False
    assert message == "整理任务已登记重试"
    assert _RetryCommand.calls[0][1]["requested_by"] == "manual_reorganize"


def test_durable_ai_button_bypasses_agent_and_requests_scheduler(monkeypatch):
    """AI 按钮命中 durable 历史时也只能登记调度重试。"""
    _install_retry_port(monkeypatch)
    history = SimpleNamespace(id=83, transfer_task_id="transfer-task-83")
    monkeypatch.setattr(
        "app.chain._transfer.get_chain_transfer_history_port",
        lambda: SimpleNamespace(get=lambda history_id: history),
    )
    monkeypatch.setattr(
        "app.chain._transfer.build_manual_redo_prompt",
        lambda _history: (_ for _ in ()).throw(
            AssertionError("durable 重试不得生成 Agent 破坏性提示词")
        ),
    )
    monkeypatch.setattr(
        "app.chain._transfer.get_task_registry",
        lambda: (_ for _ in ()).throw(
            AssertionError("durable 重试不得提交 Agent 任务")
        ),
    )
    messages = []
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(
        ai_agent_enable=False,
        history_url="/history",
    )
    chain.post_message = messages.append

    chain._take_over_transfer_history_by_ai(
        history_id=83,
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        username="tester",
    )

    assert len(messages) == 1
    assert messages[0].title == "整理任务已登记重试"
    assert _RetryCommand.calls[0][1]["requested_by"] == "ai_retry_button"


def test_durable_manual_review_rejection_does_not_fall_back_to_legacy(monkeypatch):
    """人工复核任务被拒绝后不得回退到旧识别和重整流程。"""
    _install_retry_port(monkeypatch)
    _RetryCommand.result = TransferRetryRequestResult(
        accepted=False,
        state=TransferExecutionState.MANUAL_REVIEW,
        retry_generation=1,
        message="人工复核任务必须先完成专门判定",
    )
    history = SimpleNamespace(
        id=84,
        transfer_task_id="transfer-task-84",
        src="/downloads/source.mkv",
    )
    monkeypatch.setattr(
        "app.chain._transfer.get_chain_transfer_history_port",
        lambda: SimpleNamespace(get=lambda history_id: history),
    )
    monkeypatch.setattr(
        "app.chain._transfer.Path.exists",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("拒绝后不得回退旧流程")
        ),
    )
    chain = object.__new__(TransferChain)

    state, message = chain._re_transfer(logid=84)

    assert state is False
    assert message == "人工复核任务必须先完成专门判定"
