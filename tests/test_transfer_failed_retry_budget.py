"""
有界重试预算的端到端行为验证。

app/application/history.py 的查重闸真值表与计数器 API 已在
tests/test_transfer_history_gate.py 逐项覆盖，本文件换一个角度：把「同一源路径
连续多个监控事件」串成一条时间线，验证瞬时故障能在预算内自愈、耗尽预算后被拦、
以及删除整理记录会让预算重新满额，贴近真实使用场景。
"""
from types import SimpleNamespace
from unittest.mock import Mock

from app import schemas
from app.runtime.config import settings
from app.application.history import (
    HistoryGateAction,
    TransferHistoryMutationCommand,
    clear_transfer_failures,
    evaluate_history_gate,
    failed_retry_count,
    record_transfer_failure,
)


def _reset_failed_retries(src_path, storage=None):
    """清空失败重试计数，隔离用例之间共享的模块级计数缓存。"""
    clear_transfer_failures(src_path, storage)


def _failed_history(history_id: int, src_path: str, storage: str):
    """构造一条持续失败的整理记录替身，模拟同一源路径屡次整理失败后落库的状态。"""
    return SimpleNamespace(id=history_id, status=False, src_fileitem=None,
                            src=src_path, src_storage=storage)


def test_transient_failures_self_heal_within_retry_budget(monkeypatch):
    """
    瞬时故障自愈：上限为 3 时，连续 2 次失败后第 3 个事件仍应放行重试；
    第 3 次也失败后计数达到上限，第 4 个事件应被拦截。
    """
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 3)
    src_path = "/downloads/retry-budget-self-heal.mkv"
    storage = "local"
    _reset_failed_retries(src_path, storage)
    try:
        history = _failed_history(1, src_path, storage)

        # 事件 1：整理失败，登记第 1 次失败
        record_transfer_failure(src_path, storage)
        assert failed_retry_count(src_path, storage) == 1

        # 事件 2：计数为 1，未达上限，放行重试；本次也失败，登记第 2 次失败
        assert evaluate_history_gate(history, file_size=None) == HistoryGateAction.PASS_FAILED
        record_transfer_failure(src_path, storage)
        assert failed_retry_count(src_path, storage) == 2

        # 事件 3：连续失败 2 次后，计数为 2 仍未达上限（3），第 3 个事件仍应放行重试
        assert evaluate_history_gate(history, file_size=None) == HistoryGateAction.PASS_FAILED

        # 第 3 次尝试也失败，登记第 3 次失败，计数达到上限
        record_transfer_failure(src_path, storage)
        assert failed_retry_count(src_path, storage) == 3

        # 事件 4：计数达到上限（3），应被拦截，不再自动重试
        assert evaluate_history_gate(history, file_size=None) == HistoryGateAction.SKIP_RETRY_EXHAUSTED
    finally:
        _reset_failed_retries(src_path, storage)


def test_clearing_transfer_history_restores_full_retry_budget(monkeypatch):
    """删除整理记录清零：耗尽重试预算后调用 clear_transfer_failures，应重新获得放行资格。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 2)
    src_path = "/downloads/retry-budget-cleared-by-delete.mkv"
    storage = "local"
    _reset_failed_retries(src_path, storage)
    try:
        history = _failed_history(2, src_path, storage)

        record_transfer_failure(src_path, storage)
        record_transfer_failure(src_path, storage)
        assert failed_retry_count(src_path, storage) == 2
        assert evaluate_history_gate(history, file_size=None) == HistoryGateAction.SKIP_RETRY_EXHAUSTED

        # 用户删除整理记录，显式要求重来
        clear_transfer_failures(src_path, storage)

        assert failed_retry_count(src_path, storage) == 0
        assert evaluate_history_gate(history, file_size=None) == HistoryGateAction.PASS_FAILED
    finally:
        _reset_failed_retries(src_path, storage)


def test_delete_transfer_history_endpoint_clears_retry_count(monkeypatch):
    """
    app/api/endpoints/history.py::delete_transfer_history 是用户删除整理记录的入口，
    删除时应连带清空失败重试计数，否则重整仍会受上一轮次数限制。

    该端点依赖应用命令与鉴权依赖，这里直接注入可观察命令，验证 HTTP 映射与
    应用层清理失败计数的协作，同时避免重新耦合已迁出的数据库会话参数。
    """
    from app.api.endpoints.history import delete_transfer_history

    src_path = "/downloads/retry-budget-delete-endpoint.mkv"
    storage = "local"
    history = SimpleNamespace(
        id=101,
        src=src_path,
        src_storage=storage,
        dest_fileitem=None,
        src_fileitem=None,
        download_hash=None,
    )
    repository = Mock()
    repository.get.return_value = history
    command = TransferHistoryMutationCommand(
        repository=repository,
        download_repository=Mock(),
        unit_of_work=Mock(),
        file_item_factory=lambda payload: SimpleNamespace(**payload),
        delete_media_file=Mock(return_value=True),
        publish_download_file_deleted=Mock(),
        clear_failures=clear_transfer_failures,
    )

    _reset_failed_retries(src_path, storage)
    try:
        record_transfer_failure(src_path, storage)
        record_transfer_failure(src_path, storage)
        assert failed_retry_count(src_path, storage) == 2

        response = delete_transfer_history(
            history_in=schemas.TransferHistory(id=101),
            deletesrc=False,
            deletedest=False,
            command=command,
            _="token",
        )

        assert response.success is True
        assert failed_retry_count(src_path, storage) == 0
    finally:
        _reset_failed_retries(src_path, storage)
