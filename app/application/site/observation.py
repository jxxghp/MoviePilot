"""站点搜索调用的轻量结果观察上下文。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(slots=True)
class SiteSearchObservation:
    """记录一次站点搜索是否发出请求及其可治理结果。"""

    attempted: bool = False
    outcome: str = "skipped"
    error: Optional[str] = None


_current_observation: ContextVar[Optional[SiteSearchObservation]] = ContextVar(
    "site_search_observation",
    default=None,
)


@contextmanager
def capture_site_search_observation() -> Iterator[SiteSearchObservation]:
    """为当前同步 worker 捕获一次索引器搜索结果。"""
    observation = SiteSearchObservation()
    token = _current_observation.set(observation)
    try:
        yield observation
    finally:
        _current_observation.reset(token)


def report_site_search_outcome(
    *,
    attempted: bool,
    outcome: str,
    error: Optional[str] = None,
) -> None:
    """由索引器在不改变公开返回合同的前提下发布调用结果。"""
    observation = _current_observation.get()
    if observation is None:
        return
    observation.attempted = attempted
    observation.outcome = outcome
    observation.error = error

