"""音乐识别调用的结果观察和有界请求预算，不改变来源模块的公开返回合同。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any, Iterator, Optional

BLOCKING_MUSIC_RECOGNITION_STATES = frozenset({"ambiguous", "conflict", "service_error", "budget_exhausted"})


@dataclass(slots=True)
class MusicRecognitionObservation:
    """保存一次识别的诊断与预算，嵌套来源调用共用同一请求限额。"""

    deadline: float
    request_limit: int = 8
    requests: int = 0
    status: str = "not_found"
    message: str = "没有匹配到足够可靠的音乐信息"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    lock: Any = field(default_factory=RLock, repr=False)

    @property
    def failed(self) -> bool:
        """瞬时故障或预算耗尽不能被解释为目录中没有相应作品。"""
        return self.status in {"service_error", "budget_exhausted"}

    def to_dict(self) -> dict[str, Any]:
        """输出用户可解释的结果，不包含内部时钟或同步对象。"""
        return {"status": self.status, "message": self.message,
                "requests": self.requests, "candidates": [dict(item) for item in self.candidates]}


_current_observation: ContextVar[Optional[MusicRecognitionObservation]] = ContextVar(
    "music_recognition_observation", default=None,
)


@contextmanager
def capture_music_recognition(*, seconds: float = 45, request_limit: int = 8) -> Iterator[MusicRecognitionObservation]:
    """隔离同步/异步识别上下文；嵌套入口复用预算，退出时总是恢复上层状态。"""
    existing = _current_observation.get()
    observation = existing or MusicRecognitionObservation(deadline=monotonic() + max(seconds, 0), request_limit=max(request_limit, 0))
    token = _current_observation.set(observation) if existing is None else None
    try:
        yield observation
    except Exception:
        report_music_recognition("service_error", "音乐元数据服务暂时不可用，请稍后重试")
        raise
    finally:
        if token is not None:
            _current_observation.reset(token)


def report_music_recognition(status: str, message: str = "", candidates: Optional[list[dict[str, Any]]] = None) -> None:
    """来源在不改返回值的前提下报告诊断；已有服务故障不能被空结果掩盖。"""
    observation = _current_observation.get()
    if observation is None:
        return
    with observation.lock:
        if observation.status == "budget_exhausted" and status != "budget_exhausted":
            return
        if observation.failed and status not in {"service_error", "budget_exhausted"}:
            return
        observation.status, observation.message = status, message
        if candidates is not None:
            observation.candidates = [dict(item) for item in candidates[:5]]


def music_recognition_failed() -> bool:
    """供识别与缓存调用判断本次空结果是否实际来自故障。"""
    observation = _current_observation.get()
    return bool(observation and observation.failed)


def music_recognition_diagnostics() -> dict[str, Any]:
    """读取隔离的诊断副本，供无身份返回值携带失败原因而不是伪造成功。"""
    observation = _current_observation.get()
    return observation.to_dict() if observation is not None else {}


def music_wait_allowed(delay: float) -> bool:
    """限流和重试等待也计入总预算，不为无法执行的请求继续占用未来时隙。"""
    observation = _current_observation.get()
    if observation is None or monotonic() + max(delay, 0) < observation.deadline:
        return True
    report_music_recognition("budget_exhausted", "音乐识别已达到本次等待上限，请稍后重试或手动选择专辑")
    return False


def music_request_timeout(*, claim: bool = True, maximum: float = 20) -> Optional[float]:
    """为一次真实HTTP尝试占用额度并返回剩余超时；缓存命中无需调用此函数。"""
    observation = _current_observation.get()
    if observation is None:
        return maximum
    with observation.lock:
        if observation.failed:
            return None
        remaining = observation.deadline - monotonic()
        if remaining <= 0 or (claim and observation.requests >= observation.request_limit):
            report_music_recognition("budget_exhausted", "音乐识别已达到本次请求上限，请稍后重试或手动选择专辑")
            return None
        if claim:
            observation.requests += 1
        return min(maximum, remaining)
