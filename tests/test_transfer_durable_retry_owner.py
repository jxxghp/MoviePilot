"""验证历史重试入口只把 durable 任务交给持久恢复调度器。"""

from types import SimpleNamespace

from app.application.transfer.execution import (
    TransferExecutionState,
    TransferFailureDiscardResult,
    TransferRetryRequestResult,
)
from app.chain.transfer.facade import TransferChain
from app.schemas.types import MediaSource, MediaType, NotificationChannel


class _RetryCommand:
    """记录历史入口提交的类型化重试请求。"""

    calls: list[tuple[object, dict]] = []
    result = TransferRetryRequestResult(
        accepted=True,
        state=TransferExecutionState.RETRY_WAIT,
        retry_generation=2,
        message="已提交重新整理，后台将自动处理",
    )

    def __init__(self, repository: object) -> None:
        """保存测试仓储实例。"""
        self._repository = repository

    def request_retry(self, **kwargs) -> TransferRetryRequestResult:
        """记录请求并返回用例指定结果。"""
        self.calls.append((self._repository, kwargs))
        return self.result


class _DiscardCommand:
    """记录显式重新整理提交的失败任务放弃请求。"""

    calls: list[tuple[object, dict]] = []
    result = TransferFailureDiscardResult(
        discarded=True,
        state=TransferExecutionState.FAILED,
        message="已放弃这条失败的整理任务",
    )

    def __init__(self, repository: object) -> None:
        """保存测试仓储实例。"""
        self._repository = repository

    def discard_failed(self, **kwargs) -> TransferFailureDiscardResult:
        """记录放弃请求并返回用例指定结果。"""
        self.calls.append((self._repository, kwargs))
        return self.result


def _install_retry_port(monkeypatch) -> object:
    """构造不会接触数据库的 execution 端口并安装命令替身。"""
    repository = object()
    _RetryCommand.calls = []
    _RetryCommand.result = TransferRetryRequestResult(
        accepted=True,
        state=TransferExecutionState.RETRY_WAIT,
        retry_generation=2,
        message="已提交重新整理，后台将自动处理",
    )
    monkeypatch.setattr(
        "app.chain.transfer.retry.TransferExecutionCommand",
        _RetryCommand,
    )
    return repository


def _install_discard_port(monkeypatch) -> object:
    """构造不会接触数据库的 execution 端口并安装放弃命令替身。"""
    repository = object()
    _DiscardCommand.calls = []
    _DiscardCommand.result = TransferFailureDiscardResult(
        discarded=True,
        state=TransferExecutionState.FAILED,
        message="已放弃这条失败的整理任务",
    )
    monkeypatch.setattr(
        "app.chain.transfer.records.TransferExecutionCommand",
        _DiscardCommand,
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
        "app.chain.transfer.retry.Path.exists",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("durable 重试不应检查源路径")
        ),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )
    monkeypatch.setattr(
        chain,
        "do_transfer",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable 重试不应重新准入")
        ),
    )

    state, message = chain._re_transfer(logid=81)

    assert state is True
    assert message == "已提交重新整理，后台将自动处理"
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


def test_explicit_history_redo_discards_failed_task_before_replanning(monkeypatch):
    """显式 /redo 身份应先解除旧失败任务，再执行新的识别和整理。"""
    repository = _install_discard_port(monkeypatch)
    history = SimpleNamespace(
        id=86,
        transfer_task_id="transfer-task-86",
        transfer_settlement_revision=5,
        src="/downloads/source.mkv",
        src_storage="local",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/source.mkv",
            "type": "file",
            "name": "source.mkv",
        },
        dest_fileitem=None,
        download_hash=None,
        media_source=None,
        media_id=None,
        episode_group=None,
    )
    deleted = []
    planned = []
    history_port = SimpleNamespace(
        get=lambda history_id: history,
        delete=lambda history_id: deleted.append(("history", history_id)),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository
    chain.transfer_history_repository = history_port
    chain.obtain_images = lambda **_kwargs: None
    monkeypatch.setattr(
        "app.chain.transfer.retry.Path.exists",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "app.chain.transfer.retry.MediaChain",
        lambda: SimpleNamespace(
            recognize_media=lambda **_kwargs: SimpleNamespace(title_year="Test Show")
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.records.clear_transfer_failures",
        lambda *_args: None,
    )

    def fake_do_transfer(**kwargs):
        """记录显式身份重整是否重新进入整理准入。"""
        planned.append(kwargs)
        return True, ""

    monkeypatch.setattr(chain, "do_transfer", fake_do_transfer)

    state, message = chain._re_transfer(
        logid=86,
        mtype=MediaType.TV,
        media_source=MediaSource.TMDB,
        media_id="286322",
    )

    assert state is True
    assert message == ""
    assert _DiscardCommand.calls == [
        (
            repository,
            {
                "task_id": "transfer-task-86",
                "history_id": 86,
                "settlement_revision": 5,
            },
        )
    ]
    assert deleted == [("history", 86)]
    assert planned[0]["media_source"] == MediaSource.TMDB
    assert planned[0]["media_id"] == "286322"


def test_durable_manual_cleanup_discards_task_and_removes_old_state(monkeypatch):
    """显式重整命中 FAILED durable 历史时应放弃任务并清理旧状态。"""
    repository = _install_discard_port(monkeypatch)
    history = SimpleNamespace(
        id=82,
        transfer_task_id="transfer-task-82",
        transfer_settlement_revision=4,
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
    deleted = []
    cleared = []
    history_port = SimpleNamespace(delete=lambda history_id: deleted.append(
        ("history", history_id)
    ))
    monkeypatch.setattr(
        "app.chain.transfer.records.StorageChain",
        lambda: SimpleNamespace(
            exists=lambda _fileitem: True,
            delete_media_file=lambda fileitem: deleted.append(
                ("target", fileitem.path)
            ) or True,
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.records.clear_transfer_failures",
        lambda *args: cleared.append(args),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository

    state, message = chain._delete_manual_transfer_history(
        history=history,
        transfer_history_oper=history_port,
    )

    assert state is True
    assert message == ""
    assert _DiscardCommand.calls == [
        (
            repository,
            {
                "task_id": "transfer-task-82",
                "history_id": 82,
                "settlement_revision": 4,
            },
        )
    ]
    assert deleted == [
        ("target", "/library/source.mkv"),
        ("history", 82),
    ]
    assert cleared == [("/downloads/source.mkv", "local")]


def test_durable_manual_cleanup_rejects_nonfailed_state(monkeypatch):
    """非 FAILED durable 任务被拒绝后不得删除目标、历史或失败计数。"""
    repository = _install_discard_port(monkeypatch)
    _DiscardCommand.result = TransferFailureDiscardResult(
        discarded=False,
        state=TransferExecutionState.MANUAL_REVIEW,
        message="这条整理任务需要先完成人工确认，再重试",
    )
    history = SimpleNamespace(
        id=85,
        transfer_task_id="transfer-task-85",
        transfer_settlement_revision=2,
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
    monkeypatch.setattr(
        "app.chain.transfer.records.StorageChain",
        lambda: (_ for _ in ()).throw(AssertionError("拒绝后不得删除目标")),
    )
    monkeypatch.setattr(
        "app.chain.transfer.records.clear_transfer_failures",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("拒绝后不得清理失败计数")
        ),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository
    history_port = SimpleNamespace(
        delete=lambda _history_id: (_ for _ in ()).throw(
            AssertionError("拒绝后不得删除历史")
        )
    )

    state, message = chain._delete_manual_transfer_history(
        history=history,
        transfer_history_oper=history_port,
    )

    assert state is False
    assert message == "这条整理任务需要先完成人工确认，再重试"


def test_durable_ai_button_bypasses_agent_and_requests_scheduler(monkeypatch):
    """AI 按钮命中 durable 历史时也只能登记调度重试。"""
    repository = _install_retry_port(monkeypatch)
    history = SimpleNamespace(id=83, transfer_task_id="transfer-task-83")
    monkeypatch.setattr(
        "app.chain.transfer.retry.build_manual_redo_prompt",
        lambda _history: (_ for _ in ()).throw(
            AssertionError("durable 重试不得生成 Agent 破坏性提示词")
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.retry.get_task_registry",
        lambda: (_ for _ in ()).throw(
            AssertionError("durable 重试不得提交 Agent 任务")
        ),
    )
    messages = []
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )
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
    assert messages[0].title == "已提交重新整理，后台将自动处理"
    assert _RetryCommand.calls[0][1]["requested_by"] == "ai_retry_button"


def test_durable_manual_review_rejection_does_not_fall_back_to_legacy(monkeypatch):
    """人工复核任务被拒绝后不得回退到旧识别和重整流程。"""
    repository = _install_retry_port(monkeypatch)
    _RetryCommand.result = TransferRetryRequestResult(
        accepted=False,
        state=TransferExecutionState.MANUAL_REVIEW,
        retry_generation=1,
        message="这条整理任务需要先完成人工确认，再重试",
    )
    history = SimpleNamespace(
        id=84,
        transfer_task_id="transfer-task-84",
        src="/downloads/source.mkv",
    )
    monkeypatch.setattr(
        "app.chain.transfer.retry.Path.exists",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("拒绝后不得回退旧流程")
        ),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_execution_repository = repository
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )

    state, message = chain._re_transfer(logid=84)

    assert state is False
    assert message == "这条整理任务需要先完成人工确认，再重试"
