"""
订阅下载提交前的可取消执行边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from app.runtime.log import logger
from app.schemas.common import JsonData

if TYPE_CHECKING:
    from app.domain.context import Context


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
