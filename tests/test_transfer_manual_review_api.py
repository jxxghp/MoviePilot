"""验证 durable 整理人工复核 API 的鉴权和公开契约。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.dependencies.auth import get_current_active_manage_user
from app.api.endpoints import transfer as transfer_endpoint
from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferManualReviewResult,
    TransferStepResult,
)
from app.schemas.transfer import TransferManualReviewRequest


class _ManualReviewCommand:
    """记录人工复核调用并返回可控结果。"""

    calls: list[tuple[object, dict]] = []
    result: TransferManualReviewResult | None = None
    error: Exception | None = None

    def __init__(self, repository: object) -> None:
        """保存端点取得的仓储替身。"""
        self._repository = repository

    def resolve_manual_review(self, **kwargs) -> TransferManualReviewResult:
        """记录参数，并按测试配置返回或抛出结果。"""
        self.calls.append((self._repository, kwargs))
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result


def _install_command(monkeypatch) -> object:
    """构造不接触数据库的人工复核仓储并安装命令替身。"""
    repository = object()
    _ManualReviewCommand.calls = []
    _ManualReviewCommand.error = None
    monkeypatch.setattr(
        transfer_endpoint,
        "TransferExecutionCommand",
        _ManualReviewCommand,
    )
    return repository


@pytest.mark.parametrize(
    "endpoint",
    [
        transfer_endpoint.list_transfer_manual_reviews,
        transfer_endpoint.get_transfer_manual_review,
        transfer_endpoint.resolve_transfer_manual_review,
    ],
)
def test_manual_review_endpoints_require_manage_permission(endpoint) -> None:
    """人工复核发现、详情与判定必须复用全局 manage 权限依赖。"""
    dependency = inspect.signature(
        endpoint
    ).parameters["current_user"].default.dependency

    assert dependency is get_current_active_manage_user


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"operation_id": "op-1", "decision": "failed", "reason": "确认失败"},
            "Input should be 'not_applied' or 'applied'",
        ),
        (
            {"operation_id": "op-1", "decision": "applied", "reason": "已完成"},
            "必须提供 result_payload",
        ),
        (
            {"operation_id": "op-1", "decision": "not_applied", "reason": "   "},
            "String should have at least 1 character",
        ),
    ],
)
def test_manual_review_request_rejects_unsafe_decisions(
    payload: dict,
    message: str,
) -> None:
    """公开 schema 不允许 FAILED，且 APPLIED 必须携带结果证据。"""
    with pytest.raises(ValidationError, match=message):
        TransferManualReviewRequest.model_validate(payload)


def test_manual_review_applied_wraps_result_and_hides_internal_state(
    monkeypatch,
) -> None:
    """APPLIED 应包装版本化结果，响应不得泄漏 lease 或 attempt。"""
    repository = _install_command(monkeypatch)
    _ManualReviewCommand.result = SimpleNamespace(
        task_id="task-1",
        operation_id="op-1",
        decision=TransferManualReviewDecision.APPLIED,
        state=TransferExecutionState.RETRY_WAIT,
        review_revision=4,
    )

    response = transfer_endpoint.resolve_transfer_manual_review(
        task_id="task-1",
        review=TransferManualReviewRequest(
            operation_id="op-1",
            decision="applied",
            reason="目标摘要匹配",
            result_payload={"dest_exists": True, "hash_match": True},
        ),
        current_user=SimpleNamespace(name=" admin ", username="other", id=7),
        repository=repository,
    )

    assert _ManualReviewCommand.calls == [
        (
            repository,
            {
                "task_id": "task-1",
                "operation_id": "op-1",
                "decision": TransferManualReviewDecision.APPLIED,
                "actor": "admin",
                "reason": "目标摘要匹配",
                "result": TransferStepResult(
                    payload={"dest_exists": True, "hash_match": True}
                ),
            },
        )
    ]
    assert response.data is not None
    assert response.data.model_dump() == {
        "task_id": "task-1",
        "operation_id": "op-1",
        "decision": "applied",
        "state": "retry_wait",
        "review_revision": 4,
    }
    assert "lease" not in response.model_dump_json()
    assert "attempt" not in response.model_dump_json()


def test_manual_review_not_applied_uses_username_fallback(monkeypatch) -> None:
    """名称为空时应稳定回退到 username，并允许安全重新调度。"""
    repository = _install_command(monkeypatch)
    _ManualReviewCommand.result = SimpleNamespace(
        task_id="task-2",
        operation_id="op-2",
        decision=TransferManualReviewDecision.NOT_APPLIED,
        state=TransferExecutionState.RETRY_WAIT,
        review_revision=2,
    )

    response = transfer_endpoint.resolve_transfer_manual_review(
        task_id="task-2",
        review=TransferManualReviewRequest(
            operation_id="op-2",
            decision="not_applied",
            reason="确认源文件仍存在",
        ),
        current_user=SimpleNamespace(name="", username="reviewer", id=8),
        repository=repository,
    )

    assert _ManualReviewCommand.calls[0][1]["actor"] == "reviewer"
    assert _ManualReviewCommand.calls[0][1]["result"] is None
    assert response.data is not None
    assert response.data.state == "retry_wait"


def test_manual_review_conflict_returns_http_409(monkeypatch) -> None:
    """重复或过期人工判定必须返回资源冲突而非伪成功。"""
    repository = _install_command(monkeypatch)
    _ManualReviewCommand.error = TransferExecutionConflictError("步骤已被判定")

    with pytest.raises(HTTPException) as error:
        transfer_endpoint.resolve_transfer_manual_review(
            task_id="task-1",
            review=TransferManualReviewRequest(
                operation_id="op-1",
                decision="not_applied",
                reason="重复判定",
            ),
            current_user=SimpleNamespace(name=None, username=None, id=11),
            repository=repository,
        )

    assert error.value.status_code == 409
    assert error.value.detail == "整理任务状态已变化，请刷新后重试"
    assert _ManualReviewCommand.calls[0][1]["actor"] == "11"
