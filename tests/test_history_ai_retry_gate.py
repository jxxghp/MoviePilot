"""
验证 AI 历史入口不会绕过 durable 整理任务的唯一重试权。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.endpoints import history as history_endpoint
from app.application.configuration import ApiRuntimeConfig
from app.application.transfer.execution import (
    TransferExecutionState,
    TransferFailureDiscardResult,
)
from app.schemas.history import BatchTransferHistoryRedoRequest, TransferHistory


class _HistoryQuery:
    """
    按测试输入返回脱离数据库的整理历史 DTO。
    """

    def __init__(self, histories: list[TransferHistory]) -> None:
        """
        保存按 ID 可查的测试历史。
        """
        self._histories = {history.id: history for history in histories}

    async def get_transfer(self, history_id: int) -> TransferHistory | None:
        """
        返回单条测试历史。
        """
        return self._histories.get(history_id)

    async def get_transfers(
        self,
        history_ids: list[int],
    ) -> tuple[list[TransferHistory], list[int]]:
        """
        按输入顺序返回存在和缺失的测试历史。
        """
        records = [self._histories[item] for item in history_ids if item in self._histories]
        missing = [item for item in history_ids if item not in self._histories]
        return records, missing


def _runtime(*, ai_enabled: bool = True) -> ApiRuntimeConfig:
    """
    构造历史端点需要的最小稳定配置快照。
    """
    return ApiRuntimeConfig(
        access_token_expire_minutes=30,
        btrfs_fsid_dedup=False,
        ai_agent_enable=ai_enabled,
    )


def test_transfer_history_task_id_is_internal_projection_only() -> None:
    """
    durable 任务标识可供宿主读取，但不得扩展公开历史响应。
    """
    history = TransferHistory(id=7, transfer_task_id="task-7", transfer_settlement_revision=1)

    assert history.transfer_task_id == "task-7"
    assert "transfer_task_id" not in history.model_dump()
    assert "transfer_settlement_revision" not in history.model_dump()


def _execution_port(*states):
    """
    提供只读任务快照，并监视任何意外的重试或删除写入。

    :param states: 按历史标识、任务状态组成的测试序列
    :return: 返回隔离快照的仓储替身
    """
    snapshots = {
        f"task-{history_id}": SimpleNamespace(state=state, terminal_history_id=history_id)
        for history_id, state in states
    }
    repository = Mock()
    repository.get_snapshot.side_effect = lambda *, task_id: snapshots.get(task_id)
    return repository


def test_single_ai_redo_starts_agent_without_retrying_frozen_plan(monkeypatch):
    """
    持久化失败必须真正交给 AI 分析，不能提前登记原计划重试。
    """
    repository = _execution_port((11, TransferExecutionState.FAILED))
    started = Mock()
    monkeypatch.setattr(history_endpoint, "_start_ai_redo_task", started)
    response = asyncio.run(history_endpoint.ai_redo_transfer_history(
        11, query=_HistoryQuery([TransferHistory(id=11, transfer_task_id="task-11")]),
        runtime_config=_runtime(), task_registry=object(), execution_repository=repository, _=None,
    ))
    assert response.success
    assert response.data["progress_key"].startswith("ai_redo_transfer_11_")
    assert started.call_args.kwargs["history_id"] == 11
    prompt = started.call_args.kwargs["prompt"]
    assert "task-11" in prompt and "transfer.history.replan" in prompt
    repository.request_retry.assert_not_called()
    repository.request_replan.assert_not_called()


@pytest.mark.parametrize("batch", [False, True])
def test_ai_disabled_never_retries_durable_tasks(monkeypatch, batch):
    """
    AI 未启用时单条及批量入口都拒绝，不能静默变成普通重试。
    """
    repository = Mock()
    started = Mock()
    monkeypatch.setattr(history_endpoint, "_start_ai_redo_task", started)
    monkeypatch.setattr(history_endpoint, "_start_batch_ai_redo_task", started)
    kwargs = dict(query=_HistoryQuery([TransferHistory(id=11, transfer_task_id="task-11")]),
                  runtime_config=_runtime(ai_enabled=False), task_registry=object(),
                  execution_repository=repository, _=None)
    request = (history_endpoint.batch_ai_redo_transfer_history(
        BatchTransferHistoryRedoRequest(history_ids=[11]), **kwargs
    ) if batch else history_endpoint.ai_redo_transfer_history(11, **kwargs))
    response = asyncio.run(request)
    assert not response.success and "未启用" in response.message
    assert repository.mock_calls == []
    started.assert_not_called()


@pytest.mark.parametrize("state", [TransferExecutionState.MANUAL_REVIEW,
                                  TransferExecutionState.RUNNING, TransferExecutionState.RETRY_WAIT])
def test_single_ai_redo_rejects_protected_execution_states(monkeypatch, state):
    """
    人工复核与执行中的任务不启动 Agent，也不改变现有恢复状态。
    """
    repository = _execution_port((12, state))
    started = Mock()
    monkeypatch.setattr(history_endpoint, "_start_ai_redo_task", started)
    response = asyncio.run(history_endpoint.ai_redo_transfer_history(
        12, query=_HistoryQuery([TransferHistory(id=12, transfer_task_id="task-12")]),
        runtime_config=_runtime(), task_registry=object(), execution_repository=repository, _=None,
    ))
    assert not response.success
    assert "人工确认" in response.message if state is TransferExecutionState.MANUAL_REVIEW else "正在处理" in response.message
    started.assert_not_called()
    repository.request_retry.assert_not_called()


@pytest.mark.parametrize("mixed", [False, True])
def test_batch_ai_redo_sends_all_eligible_records_to_agent(monkeypatch, mixed):
    """
    全持久化或混合批量均交给 AI，保留前端进度协议并按输入去重。
    """
    repository = _execution_port((18, TransferExecutionState.FAILED), (19, TransferExecutionState.FAILED))
    started = Mock()
    monkeypatch.setattr(history_endpoint, "_start_batch_ai_redo_task", started)
    response = asyncio.run(history_endpoint.batch_ai_redo_transfer_history(
        BatchTransferHistoryRedoRequest(history_ids=[18, 19, 18]),
        query=_HistoryQuery([TransferHistory(id=18, transfer_task_id="task-18"),
                             TransferHistory(id=19, transfer_task_id=None if mixed else "task-19")]),
        runtime_config=_runtime(), task_registry=object(), execution_repository=repository, _=None,
    ))
    assert response.success
    assert response.data["history_ids"] == [18, 19]
    assert response.data["progress_key"].startswith("ai_redo_transfer_batch_")
    assert started.call_args.kwargs["history_ids"] == [18, 19]
    assert "task-18" in started.call_args.kwargs["prompt"]
    repository.request_retry.assert_not_called()


def test_batch_rejection_has_no_partial_retry_side_effects(monkeypatch):
    """
    批量任一任务仍在执行时，整批只读拒绝，不先重试其它任务。
    """
    repository = _execution_port((21, TransferExecutionState.FAILED), (22, TransferExecutionState.RUNNING))
    started = Mock()
    monkeypatch.setattr(history_endpoint, "_start_batch_ai_redo_task", started)
    response = asyncio.run(history_endpoint.batch_ai_redo_transfer_history(
        BatchTransferHistoryRedoRequest(history_ids=[21, 22, 23]),
        query=_HistoryQuery([TransferHistory(id=21, transfer_task_id="task-21"),
                             TransferHistory(id=22, transfer_task_id="task-22"), TransferHistory(id=23)]),
        runtime_config=_runtime(), task_registry=object(), execution_repository=repository, _=None,
    ))
    assert not response.success and "第 22 条" in response.message
    started.assert_not_called()
    repository.request_retry.assert_not_called()


@pytest.mark.parametrize("discarded", [True, False])
def test_discard_corrupt_history_returns_typed_result(discarded):
    """
    清理端点保留历史 ID，并透传持久层的清理成功或拒绝结果。
    """
    repository = Mock()
    repository.discard_corrupt_by_history.return_value = TransferFailureDiscardResult(
        discarded=discarded, state=None, message="清理结果",
    )
    response = history_endpoint.discard_corrupt_transfer_history(
        history_id=7,
        history=TransferHistory(id=7, transfer_task_id="task-7"),
        execution_repository=repository,
        _=None,
    )
    assert response.success is discarded
    assert response.message == "清理结果"
    assert response.data.model_dump() == {"history_id": 7}
    repository.discard_corrupt_by_history.assert_called_once_with(task_id="task-7", history_id=7)


@pytest.mark.parametrize("exists", [True, False])
def test_discard_corrupt_history_without_task_is_idempotent(exists):
    """
    已清理历史返回同一结构，缺失历史返回失败且均不调用持久层清理。
    """
    repository = Mock()
    response = history_endpoint.discard_corrupt_transfer_history(
        history_id=7,
        history=TransferHistory(id=7) if exists else None,
        execution_repository=repository,
        _=None,
    )
    assert response.success is exists
    if exists:
        assert response.data.model_dump() == {"history_id": 7}
        assert response.message == "整理任务已清理"
    else:
        assert response.data is None
        assert response.message == "整理记录不存在"
    repository.discard_corrupt_by_history.assert_not_called()


def test_discard_corrupt_history_dependency_loads_dto():
    """
    同步清理前通过异步依赖读取历史，避免在事件循环运行同步写事务。
    """
    result = asyncio.run(history_endpoint._get_discard_transfer_history(
        history_id=7, query=_HistoryQuery([TransferHistory(id=7)]),
    ))
    assert result.id == 7


@pytest.mark.parametrize("success", [True, False])
def test_resolve_transfer_cleanup_returns_command_result(success):
    """
    下载器人工清理确认端点应透传命令结果且只操作指定历史。
    """
    command = Mock()
    command.resolve_cleanup.return_value = SimpleNamespace(
        success=success,
        message="清理确认结果",
    )

    response = history_endpoint.resolve_transfer_cleanup(
        history_id=9,
        command=command,
        _=None,
    )

    assert response.success is success
    assert response.message == "清理确认结果"
    command.resolve_cleanup.assert_called_once_with(9)


@pytest.mark.parametrize('batch', [False, True])
def test_ai_background_progress_reports_analysis_completion(monkeypatch, batch):
    """
    单条与批量后台任务均真实调用 Agent，并将分析结束与文件成功区分。
    """
    from unittest.mock import AsyncMock

    from app.runtime.tasks import TaskRegistry

    progress = SimpleNamespace(start=AsyncMock(), update=AsyncMock(), end=AsyncMock())
    manager = SimpleNamespace(run_background_prompt=AsyncMock())
    monkeypatch.setattr(history_endpoint, 'AsyncProgressHelper', lambda _key: progress)
    monkeypatch.setattr(history_endpoint, 'get_running_agent_manager', lambda: manager)

    async def scenario():
        """
        等待受管任务完成，避免遗留后台协程。
        """
        registry = TaskRegistry()
        kwargs = dict(prompt='analyze and verify S02E03', progress_key='test-ai', task_registry=registry)
        if batch:
            history_endpoint._start_batch_ai_redo_task(history_ids=[11, 12], **kwargs)
        else:
            history_endpoint._start_ai_redo_task(history_id=11, **kwargs)
        await asyncio.gather(*(record.task for record in registry.records))
        assert await registry.shutdown()

    asyncio.run(scenario())
    manager.run_background_prompt.assert_awaited_once()
    assert manager.run_background_prompt.call_args.kwargs['message'] == 'analyze and verify S02E03'
    assert '分析完成' in progress.update.call_args.kwargs['text']
    progress.end.assert_awaited_once()
