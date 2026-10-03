"""音乐识别调用的结果观察和有界请求预算，不改变来源模块的公开返回合同。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any, Iterator, Optional

BLOCKING_MUSIC_RECOGNITION_STATES = frozenset({"ambiguous", "conflict", "service_error", "budget_exhausted"})
RETRYABLE_MUSIC_RECOGNITION_STATES = frozenset({"service_error", "budget_exhausted"})


@dataclass(slots=True)
class MusicRecognitionObservation:
    """保存一次识别的诊断与预算，嵌套来源调用共用同一请求限额。"""

    deadline: float
    request_limit: int = 8
    requests: int = 0
    status: str = "not_found"
    message: str = "没有匹配到足够可靠的音乐信息"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    fingerprint_candidates: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    parent: Optional["MusicRecognitionObservation"] = field(default=None, repr=False)
    lock: Any = field(default_factory=RLock, repr=False)

    @property
    def failed(self) -> bool:
        """瞬时故障或预算耗尽不能被解释为目录中没有相应作品。"""
        return self.status in RETRYABLE_MUSIC_RECOGNITION_STATES

    def to_dict(self) -> dict[str, Any]:
        """输出用户可解释的结果，不包含内部时钟或同步对象。"""
        result = {"status": self.status, "message": self.message,
                  "requests": self.requests, "candidates": [dict(item) for item in self.candidates]}
        if self.sources:
            result["sources"] = [dict(item) for item in self.sources]
        return result


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


@contextmanager
def capture_music_source(source: str) -> Iterator[MusicRecognitionObservation]:
    """每个来源独立记录失败，全部祖先预算在同一锁下计数，不能通过换来源重置总额度。"""
    parent = _current_observation.get()
    observation = MusicRecognitionObservation(
        deadline=min(parent.deadline, monotonic() + 45) if parent else monotonic() + 45,
        parent=parent, lock=parent.lock if parent else RLock(),
    )
    if parent and parent.status in BLOCKING_MUSIC_RECOGNITION_STATES:
        observation.status, observation.message = parent.status, parent.message
        observation.candidates = [dict(item) for item in parent.candidates]
    token = _current_observation.set(observation)
    try:
        yield observation
    except Exception:
        report_music_recognition("service_error", "音乐元数据来源暂时不可用")
        raise
    finally:
        _current_observation.reset(token)
        if parent is not None:
            with parent.lock:
                parent.sources = [*parent.sources, {"source": source, **observation.to_dict()}][-8:]


def _result_diagnostic(info: Any) -> dict[str, Any]:
    """只读取音乐结果的诊断，避免改变影视识别的插件及共享回退行为。"""
    if not hasattr(info, "music_type"):
        return {}
    raw = getattr(info, "raw_data", None)
    diagnostic = raw.get("recognition") if isinstance(raw, dict) else None
    return diagnostic if isinstance(diagnostic, dict) else {}


def music_recognition_needs_confirmation(info: Any) -> bool:
    """明确的歧义或冲突需要用户确认，不能由下一层弱搜索或共享结果覆盖。"""
    return _result_diagnostic(info).get("status") in {"ambiguous", "conflict"}


def music_recognition_is_blocked(info: Any) -> bool:
    """来源及插件回退均未解决的诊断，必须保留给整理准入和持久重试处理。"""
    return _result_diagnostic(info).get("status") in BLOCKING_MUSIC_RECOGNITION_STATES


def _observation_ancestors(observation: MusicRecognitionObservation) -> list[MusicRecognitionObservation]:
    """收集当前来源及外层操作预算；父引用只在创建子范围时建立。"""
    scopes = []
    current: Optional[MusicRecognitionObservation] = observation
    while current is not None:
        scopes.append(current)
        current = current.parent
    return scopes


def report_music_recognition(status: str, message: str = "", candidates: Optional[list[dict[str, Any]]] = None) -> None:
    """来源在不改返回值的前提下报告诊断；已有服务故障不能被空结果掩盖。"""
    observation = _current_observation.get()
    if observation is None:
        return
    with observation.lock:
        confirmations = {"ambiguous", "conflict"}
        if observation.status in confirmations and status not in confirmations:
            return
        if observation.status == "budget_exhausted" and status not in {"budget_exhausted", *confirmations}:
            return
        if observation.failed and status not in {"service_error", "budget_exhausted", *confirmations}:
            return
        observation.status, observation.message = status, message
        if candidates is not None:
            observation.candidates = [dict(item) for item in candidates[:5]]


def music_recognition_failed() -> bool:
    """检查当前来源及祖先故障，子范围或缓存命中不能绕过已失败的外层操作。"""
    observation = _current_observation.get()
    return bool(observation and any(scope.failed for scope in _observation_ancestors(observation)))


def music_recognition_diagnostics() -> dict[str, Any]:
    """读取隔离的诊断副本，供无身份返回值携带失败原因而不是伪造成功。"""
    observation = _current_observation.get()
    return observation.to_dict() if observation is not None else {}


def report_music_fingerprints(candidates: list[dict[str, Any]]) -> None:
    """保留指纹原始候选证据，不把指纹关联提前宣布为已确认录音。"""
    observation = _current_observation.get()
    if observation is not None:
        with observation.lock:
            observation.fingerprint_candidates = [dict(item) for item in candidates[:5]]


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
        scopes = _observation_ancestors(observation)
        if any(scope.status in BLOCKING_MUSIC_RECOGNITION_STATES for scope in scopes):
            return None
        remaining = min(scope.deadline for scope in scopes) - monotonic()
        if remaining <= 0 or (claim and any(scope.requests >= scope.request_limit for scope in scopes)):
            report_music_recognition("budget_exhausted", "音乐识别已达到本次请求上限，请稍后重试或手动选择专辑")
            return None
        if claim:
            for scope in scopes:
                scope.requests += 1
        return min(maximum, remaining)
