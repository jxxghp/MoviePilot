"""验证 AI 历史入口不会绕过 durable 整理任务的唯一重试权。"""

from __future__ import annotations

import asyncio

from app.api.endpoints import history as history_endpoint
from app.application.configuration import ApiRuntimeConfig
from app.application.transfer.execution import (
    TransferExecutionState,
    TransferRetryRequestResult,
)
from app.runtime.progress import AsyncProgressHelper
from app.schemas.history import BatchTransferHistoryRedoRequest, TransferHistory


class _HistoryQuery:
    """按测试输入返回脱离数据库的整理历史 DTO。"""

    def __init__(self, histories: list[TransferHistory]) -> None:
        """保存按 ID 可查的测试历史。"""
        self._histories = {history.id: history for history in histories}

    async def get_transfer(self, history_id: int) -> TransferHistory | None:
        """返回单条测试历史。"""
        return self._histories.get(history_id)

    async def get_transfers(
        self,
        history_ids: list[int],
    ) -> tuple[list[TransferHistory], list[int]]:
        """按输入顺序返回存在和缺失的测试历史。"""
        records = [self._histories[item] for item in history_ids if item in self._histories]
        missing = [item for item in history_ids if item not in self._histories]
        return records, missing


class _RetryCommand:
    """记录 execution 重试请求并返回逐任务测试结果。"""

    calls: list[tuple[object, dict]] = []
    results: dict[str, TransferRetryRequestResult] = {}

    def __init__(self, repository: object) -> None:
        """保存调用方取得的 execution 仓储。"""
        self._repository = repository

    def request_retry(self, **kwargs) -> TransferRetryRequestResult:
        """记录调用并返回任务对应结果。"""
        self.calls.append((self._repository, kwargs))
        return self.results[kwargs["task_id"]]


async def _record_async(target: list[dict], payload: dict) -> None:
    """记录被 await 的异步边界调用。"""
    target.append(payload)


def _runtime(*, ai_enabled: bool = True) -> ApiRuntimeConfig:
    """构造历史端点需要的最小稳定配置快照。"""
    return ApiRuntimeConfig(
        access_token_expire_minutes=30,
        btrfs_fsid_dedup=False,
        ai_agent_enable=ai_enabled,
    )


def _retry_result(
    *,
    accepted: bool,
    state: TransferExecutionState,
    message: str,
) -> TransferRetryRequestResult:
    """构造 execution 重试登记结果。"""
    return TransferRetryRequestResult(
        accepted=accepted,
        state=state,
        retry_generation=3,
        message=message,
    )


def _install_retry_command(monkeypatch, results: dict[str, TransferRetryRequestResult]) -> object:
    """构造不会接触真实数据库的 execution 端口并安装命令替身。"""
    repository = object()
    _RetryCommand.calls = []
    _RetryCommand.results = results
    monkeypatch.setattr(history_endpoint, "TransferExecutionCommand", _RetryCommand)
    return repository


def test_transfer_history_task_id_is_internal_projection_only() -> None:
    """durable 任务标识可供宿主读取，但不得扩展公开历史响应。"""
    history = TransferHistory(id=7, transfer_task_id="task-7")

    assert history.transfer_task_id == "task-7"
    assert "transfer_task_id" not in history.model_dump()


def test_durable_retry_progress_is_immediately_completed_for_existing_sse() -> None:
    """durable 重试完成进度应让既有 SSE 客户端首次读取即可收口。"""

    async def scenario() -> dict:
        """写入并回读同一个测试进度键。"""
        progress_key = "test_history_durable_retry_completed"
        await history_endpoint._complete_durable_retry_progress(
            progress_key=progress_key,
            text="已提交重新整理，后台将自动处理",
            history_ids=[8],
        )
        detail = await AsyncProgressHelper(progress_key).get()
        assert detail is not None
        return detail

    detail = asyncio.run(scenario())

    assert detail["enable"] is False
    assert detail["value"] == 100
    assert detail["data"]["history_ids"] == [8]
    assert detail["data"]["success"] is True
    assert detail["data"]["completed"] is True
    assert detail["data"]["message"] == "已提交重新整理，后台将自动处理"


def test_single_ai_redo_requests_durable_retry_without_agent(monkeypatch) -> None:
    """单条 durable AI 重做只登记调度重试，即使 Agent 功能未启用。"""
    repository = _install_retry_command(
        monkeypatch,
        {
            "task-11": _retry_result(
                accepted=True,
                state=TransferExecutionState.RETRY_WAIT,
                message="已提交重新整理，后台将自动处理",
            )
        },
    )
    monkeypatch.setattr(
        history_endpoint,
        "_start_ai_redo_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("durable 重试不得启动 Agent")),
    )
    completed_progress: list[dict] = []
    monkeypatch.setattr(
        history_endpoint,
        "_complete_durable_retry_progress",
        lambda **kwargs: _record_async(completed_progress, kwargs),
    )

    response = asyncio.run(
        history_endpoint.ai_redo_transfer_history(
            11,
            query=_HistoryQuery([TransferHistory(id=11, transfer_task_id="task-11")]),
            runtime_config=_runtime(ai_enabled=False),
            task_registry=object(),
            execution_repository=repository,
            _=object(),
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data["progress_key"].startswith("transfer_retry_11_")
    assert response.message == "已提交重新整理，后台将自动处理"
    assert completed_progress[0]["history_ids"] == [11]
    assert _RetryCommand.calls == [
        (
            repository,
            {
                "task_id": "task-11",
                "reason": "AI REST 请求重试整理历史 #11",
                "requested_by": "history_ai_redo",
            },
        )
    ]


def test_single_ai_redo_reports_manual_review_rejection(monkeypatch) -> None:
    """人工复核状态必须原样拒绝，且不得回退到破坏性 Agent 流程。"""
    repository = _install_retry_command(
        monkeypatch,
        {
            "task-12": _retry_result(
                accepted=False,
                state=TransferExecutionState.MANUAL_REVIEW,
                message="这条整理任务需要先完成人工确认，再重试",
            )
        },
    )
    monkeypatch.setattr(
        history_endpoint,
        "build_manual_redo_prompt",
        lambda _history: (_ for _ in ()).throw(AssertionError("拒绝后不得生成 Agent 提示词")),
    )

    response = asyncio.run(
        history_endpoint.ai_redo_transfer_history(
            12,
            query=_HistoryQuery([TransferHistory(id=12, transfer_task_id="task-12")]),
            runtime_config=_runtime(),
            task_registry=object(),
            execution_repository=repository,
            _=object(),
        )
    )

    assert response.success is False
    assert response.message == "这条整理任务需要先完成人工确认，再重试"


def test_batch_ai_redo_returns_completed_progress_for_durable_tasks(monkeypatch) -> None:
    """全 durable 批量接受后保留前端既有 progress_key 协议。"""
    repository = _install_retry_command(
        monkeypatch,
        {
            "task-18": _retry_result(
                accepted=True,
                state=TransferExecutionState.RETRY_WAIT,
                message="已提交重新整理，后台将自动处理",
            ),
            "task-19": _retry_result(
                accepted=True,
                state=TransferExecutionState.RETRY_WAIT,
                message="整理任务已在等待重新处理",
            ),
        },
    )
    completed_progress: list[dict] = []
    monkeypatch.setattr(
        history_endpoint,
        "_complete_durable_retry_progress",
        lambda **kwargs: _record_async(completed_progress, kwargs),
    )
    monkeypatch.setattr(
        history_endpoint,
        "_start_batch_ai_redo_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("全 durable 批量不得启动 Agent")),
    )

    response = asyncio.run(
        history_endpoint.batch_ai_redo_transfer_history(
            BatchTransferHistoryRedoRequest(history_ids=[18, 19]),
            query=_HistoryQuery(
                [
                    TransferHistory(id=18, transfer_task_id="task-18"),
                    TransferHistory(id=19, transfer_task_id="task-19"),
                ]
            ),
            runtime_config=_runtime(ai_enabled=False),
            task_registry=object(),
            execution_repository=repository,
            _=object(),
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data["progress_key"].startswith("transfer_retry_batch_")
    assert response.data["history_ids"] == [18, 19]
    assert completed_progress[0]["history_ids"] == [18, 19]


def test_batch_ai_redo_reports_each_rejection_without_starting_legacy_agent(
    monkeypatch,
) -> None:
    """混合批量逐 task 返回拒绝，避免同时产生无人监听的旧 Agent 任务。"""
    repository = _install_retry_command(
        monkeypatch,
        {
            "task-21": _retry_result(
                accepted=True,
                state=TransferExecutionState.RETRY_WAIT,
                message="已提交重新整理，后台将自动处理",
            ),
            "task-22": _retry_result(
                accepted=False,
                state=TransferExecutionState.RUNNING,
                message="这条整理任务当前无法重试，请刷新后再试",
            ),
        },
    )
    started: list[dict] = []
    prompted: list[list[int]] = []
    monkeypatch.setattr(
        history_endpoint,
        "build_batch_manual_redo_prompt",
        lambda histories: prompted.append([history.id for history in histories]) or "legacy prompt",
    )
    monkeypatch.setattr(
        history_endpoint,
        "_start_batch_ai_redo_task",
        lambda **kwargs: started.append(kwargs),
    )
    histories = [
        TransferHistory(id=21, transfer_task_id="task-21"),
        TransferHistory(id=22, transfer_task_id="task-22"),
        TransferHistory(id=23),
    ]

    response = asyncio.run(
        history_endpoint.batch_ai_redo_transfer_history(
            BatchTransferHistoryRedoRequest(history_ids=[21, 22, 23]),
            query=_HistoryQuery(histories),
            runtime_config=_runtime(),
            task_registry=object(),
            execution_repository=repository,
            _=object(),
        )
    )

    assert response.success is False
    assert "已提交 1 个整理任务，后台将自动处理" in response.message
    assert "第 22 条：这条整理任务当前无法重试，请刷新后再试" in response.message
    assert response.data is None
    assert "1 条旧历史未提交" in response.message
    assert prompted == []
    assert [call[1]["task_id"] for call in _RetryCommand.calls] == [
        "task-21",
        "task-22",
    ]
    assert started == []


def test_batch_ai_redo_sends_only_legacy_records_after_durable_acceptance(
    monkeypatch,
) -> None:
    """混合批量全接受时 durable 只登记重试，旧历史才进入 Agent。"""
    repository = _install_retry_command(
        monkeypatch,
        {
            "task-24": _retry_result(
                accepted=True,
                state=TransferExecutionState.RETRY_WAIT,
                message="已提交重新整理，后台将自动处理",
            )
        },
    )
    prompted: list[list[int]] = []
    started: list[dict] = []
    monkeypatch.setattr(
        history_endpoint,
        "build_batch_manual_redo_prompt",
        lambda histories: prompted.append([history.id for history in histories]) or "legacy prompt",
    )
    monkeypatch.setattr(
        history_endpoint,
        "_start_batch_ai_redo_task",
        lambda **kwargs: started.append(kwargs),
    )

    response = asyncio.run(
        history_endpoint.batch_ai_redo_transfer_history(
            BatchTransferHistoryRedoRequest(history_ids=[24, 25]),
            query=_HistoryQuery(
                [
                    TransferHistory(id=24, transfer_task_id="task-24"),
                    TransferHistory(id=25),
                ]
            ),
            runtime_config=_runtime(),
            task_registry=object(),
            execution_repository=repository,
            _=object(),
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data["history_ids"] == [24, 25]
    assert prompted == [[25]]
    assert started[0]["history_ids"] == [25]
