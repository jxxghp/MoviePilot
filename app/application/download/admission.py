"""订阅下载提交的持久幂等与不确定终态合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol


class DownloadReconciliationRequired(RuntimeError):
    """表示下载器可能已接受任务，必须先对账才能继续自动提交。"""


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadGovernance:
    """把订阅身份、入口任务与取消检查传入下载提交边界。"""

    subscription_id: int
    mode: str
    task_id: Optional[str] = None
    cancelled: Optional[Callable[[], bool]] = None
    mark_started: Optional[Callable[[], None]] = None


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadRequest:
    """一次订阅下载提交认领所需的规范身份。"""

    idempotency_key: str
    subscription_id: int
    task_id: Optional[str]
    logical_identity: str
    resource_key: str
    coverage: str
    mode: str


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadSnapshot:
    """脱离 Session 的订阅下载提交状态快照。"""

    idempotency_key: str
    subscription_id: int
    task_id: Optional[str]
    state: str
    attempt_count: int
    attempt_token: Optional[str]
    downloader: Optional[str]
    download_hash: Optional[str]
    available_at: Optional[str]
    last_error: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadClaim:
    """返回本次是否取得唯一提交权以及当前持久状态。"""

    acquired: bool
    snapshot: SubscriptionDownloadSnapshot


class SubscriptionDownloadRepository(Protocol):
    """订阅下载幂等账本所需的最小持久化端口。"""

    def claim(self, request: SubscriptionDownloadRequest) -> SubscriptionDownloadClaim:
        """按唯一键认领提交；仅到期 retryable/cancelled 状态允许重新认领。"""
        ...

    def mark_accepted(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        downloader: Optional[str],
        download_hash: str,
    ) -> bool:
        """记录下载器已明确接受任务，后续任何失败都不得自动重试。"""
        ...

    def mark_succeeded(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
    ) -> bool:
        """在 canonical 本地结算完成后写入成功终态。"""
        ...

    def mark_retryable(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        available_at: str,
        error: Optional[str],
    ) -> bool:
        """记录下载器明确拒绝、尚未产生外部副作用的可重试状态。"""
        ...

    def mark_reconcile_required(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        error: Optional[str],
        downloader: Optional[str] = None,
        download_hash: Optional[str] = None,
    ) -> bool:
        """冻结可能已产生外部副作用的提交，等待下载器对账。"""
        ...

    def mark_cancelled(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
    ) -> bool:
        """仅在进入下载器副作用前收口取消。"""
        ...

    def has_started_for_task(self, task_id: str) -> bool:
        """判断搜索任务是否已有不能按未执行处理的下载提交。"""
        ...
