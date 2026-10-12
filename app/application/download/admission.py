"""订阅下载提交前的可取消执行边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from app.runtime.log import logger

if TYPE_CHECKING:
    from app.domain.context import Context


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadGovernance:
    """把取消检查与副作用起点回传给订阅执行上下文。"""

    cancelled: Optional[Callable[[], bool]] = None
    mark_started: Optional[Callable[[], None]] = None
    decision_observer: Optional[Callable[[Context, str, Optional[str]], None]] = None

    def report(self, context: Context, decision: str, reason: Optional[str] = None) -> None:
        """报告安全业务结果；观测失败不得让已提交的下载被再次执行。"""
        if self.decision_observer is not None:
            try:
                self.decision_observer(context, decision, reason)
            except Exception:
                logger.error("订阅下载活动记录失败", exc_info=True)
