"""订阅下载提交前的可取消执行边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True, slots=True)
class SubscriptionDownloadGovernance:
    """把取消检查与副作用起点回传给订阅执行上下文。"""

    cancelled: Optional[Callable[[], bool]] = None
    mark_started: Optional[Callable[[], None]] = None
