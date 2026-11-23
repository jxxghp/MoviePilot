"""
下载提交前的授权复核与订阅取消边界。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from app.runtime.log import logger
from app.schemas.common import JsonData

if TYPE_CHECKING:
    from app.domain.context import Context


_download_authorization: ContextVar[Callable[[], None] | None] = ContextVar("download_authorization", default=None)


class DownloadAuthorizationError(PermissionError):
    """
    媒体准备期间失去下载权限，当前提交应中止。
    """


@contextmanager
def scoped_download_authorization(check: Callable[[], None] | None) -> Iterator[None]:
    """
    在当前下载请求内安装执行权限检查。

    :param check: 权限失效时抛出 PermissionError 的检查，None 清除继承的检查
    """
    token = _download_authorization.set(check)
    try:
        yield
    finally:
        _download_authorization.reset(token)


def get_download_authorization() -> Callable[[], None] | None:
    """
    读取本次下载提交的权限检查。
    """
    return _download_authorization.get()


def verify_download_authorization(check: Callable[[], None] | None) -> None:
    """
    执行本次提交的权限检查，并把撤权与下载器故障区分。

    :param check: 下载入口提供的实时校验，旧入口可以为空
    """
    if check is not None:
        try:
            check()
        except PermissionError as error:
            raise DownloadAuthorizationError(str(error)) from error


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadGovernance:
    """
    把取消检查与副作用起点回传给订阅执行上下文。

    :param cancelled: 检查订阅是否已取消的回调；None 表示无外部取消源
    :param mark_started: 首次产生下载副作用时通知执行上下文的回调
    :param prepare_submission: 最终选集确定后、调用下载器前冻结订阅提交来源的回调
    :param decision_observer: 接收资源上下文、决策和原因的观测回调；None 表示不报告
    """

    cancelled: Optional[Callable[[], bool]] = None
    mark_started: Optional[Callable[[], None]] = None
    decision_observer: Optional[Callable[[Context, str, Optional[str]], None]] = None

    prepare_submission: Optional[Callable[[Context], None]] = None

    def report(self, context: Context, decision: str, reason: Optional[str] = None) -> None:
        """
        报告安全业务结果；观测失败不得让已提交的下载被再次执行。
        """
        if self.decision_observer is not None:
            try:
                self.decision_observer(context, decision, reason)
            except Exception:
                logger.error("订阅下载活动记录失败", exc_info=True)


def subscription_download_label(submission: dict[str, JsonData] | None) -> str | None:
    """
    为明确记录的订阅提交生成通知与日志标签，缺失标记不推断历史来源。
    """
    if not submission:
        return None
    if submission.get("submission_type") == "initial":
        return "首次订阅"
    if submission.get("submission_type") == "upgrade":
        return "全集洗版" if submission.get("best_version_full") else "洗版升级"
    return None
