"""低基数运行指标端口与进程级 Facade。"""

from __future__ import annotations

import time
import functools
import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterator, Mapping, Protocol, TypeVar, cast


class MetricKind(StrEnum):
    """声明指标采用 counter、histogram 或 gauge 语义。"""

    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """声明稳定指标名称、类型与允许的低基数标签。"""

    name: str
    kind: MetricKind
    labels: frozenset[str]


METRIC_SPECS = {
    spec.name: spec
    for spec in (
        MetricSpec("http.server.duration", MetricKind.HISTOGRAM, frozenset({"route", "method", "status"})),
        MetricSpec("db.pool.wait", MetricKind.HISTOGRAM, frozenset({"backend", "outcome"})),
        MetricSpec("db.pool.checked_out", MetricKind.GAUGE, frozenset({"backend"})),
        MetricSpec("db.pool.timeout", MetricKind.COUNTER, frozenset({"backend"})),
        MetricSpec("event.queue.depth", MetricKind.GAUGE, frozenset({"delivery"})),
        MetricSpec("event.handler.duration", MetricKind.HISTOGRAM, frozenset({"event_type", "handler_type", "outcome"})),
        MetricSpec("module.provider.duration", MetricKind.HISTOGRAM, frozenset({"method", "provider_type", "outcome"})),
        MetricSpec("module.provider.timeout", MetricKind.COUNTER, frozenset({"method", "provider_type"})),
        MetricSpec(
            "module.contract.legacy_hit",
            MetricKind.COUNTER,
            frozenset({"method", "caller_type", "abi_source"}),
        ),
        MetricSpec(
            "compat.facade.hit",
            MetricKind.COUNTER,
            frozenset({"facade", "operation", "visibility", "abi_source"}),
        ),
        MetricSpec("scheduler.job.duration", MetricKind.HISTOGRAM, frozenset({"owner", "outcome"})),
        MetricSpec("scheduler.job.overlap_skip", MetricKind.COUNTER, frozenset({"owner"})),
        MetricSpec("scheduler.job.retry", MetricKind.COUNTER, frozenset({"owner"})),
        MetricSpec("scheduler.job.dead_letter", MetricKind.COUNTER, frozenset({"owner"})),
        MetricSpec("plugin.lifecycle.duration", MetricKind.HISTOGRAM, frozenset({"operation", "outcome"})),
        MetricSpec("agent.active_tasks", MetricKind.GAUGE, frozenset({"task_type"})),
        MetricSpec("agent.cancel", MetricKind.COUNTER, frozenset({"task_type", "outcome"})),
        MetricSpec("agent.provider.duration", MetricKind.HISTOGRAM, frozenset({"provider_type", "outcome"})),
        MetricSpec("agent.token_usage", MetricKind.COUNTER, frozenset({"provider_type", "direction"})),
    )
}


class ObservationPort(Protocol):
    """Application/runtime 可依赖的最小指标写入端口。"""

    def record(self, spec: MetricSpec, value: float, labels: Mapping[str, str]) -> None:
        """记录一个已经通过标签合同校验的指标值。"""


class NoopObservationPort:
    """未安装或未启用 exporter 时完全无副作用的默认实现。"""

    def record(self, spec: MetricSpec, value: float, labels: Mapping[str, str]) -> None:
        """接受合法指标但不分配 exporter 资源。"""


_observation_port: ObservationPort = NoopObservationPort()

_FacadeClass = TypeVar("_FacadeClass", bound=type)


def observe_compat_facade(facade: str) -> Callable[[_FacadeClass], _FacadeClass]:
    """为旧 ABI Facade 的公开和私有方法记录低基数命中，不改变方法合同。"""

    def decorate(cls: _FacadeClass) -> _FacadeClass:
        for name, descriptor in tuple(vars(cls).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            visibility = "private" if name.startswith("_") else "public"
            if isinstance(descriptor, classmethod):
                wrapped = _wrap_compat_method(
                    descriptor.__func__, facade, name, visibility
                )
                setattr(cls, name, classmethod(wrapped))
            elif isinstance(descriptor, staticmethod):
                wrapped = _wrap_compat_method(
                    descriptor.__func__, facade, name, visibility
                )
                setattr(cls, name, staticmethod(wrapped))
            elif callable(descriptor):
                setattr(
                    cls,
                    name,
                    _wrap_compat_method(descriptor, facade, name, visibility),
                )
        return cls

    return decorate


def _wrap_compat_method(
    method: Callable[..., Any],
    facade: str,
    operation: str,
    visibility: str,
) -> Callable[..., Any]:
    """包装一个兼容方法并保持同步/异步调用形态及反射元数据。"""
    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            record_metric(
                "compat.facade.hit",
                facade=facade,
                operation=operation,
                visibility=visibility,
                abi_source="legacy_facade",
            )
            return await method(*args, **kwargs)

        return cast(Callable[..., Any], async_wrapper)

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        record_metric(
            "compat.facade.hit",
            facade=facade,
            operation=operation,
            visibility=visibility,
            abi_source="legacy_facade",
        )
        return method(*args, **kwargs)

    return cast(Callable[..., Any], wrapper)


def configure_observation(port: ObservationPort | None) -> None:
    """由组合根替换进程级端口；None 明确恢复 no-op。"""
    global _observation_port
    _observation_port = port or NoopObservationPort()


def record_metric(name: str, value: float = 1, **labels: str) -> None:
    """校验名称、类型无关数值和标签白名单后写入当前端口。"""
    spec = METRIC_SPECS[name]
    unexpected = set(labels) - spec.labels
    if unexpected:
        raise ValueError(f"指标 {name} 包含未登记标签：{sorted(unexpected)}")
    _observation_port.record(spec, float(value), labels)


@contextmanager
def observe_duration(name: str, **labels: str) -> Iterator[None]:
    """记录代码块耗时，并把成功或失败收敛为低基数 outcome。"""
    started_at = time.perf_counter()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        duration_labels = dict(labels)
        if "outcome" in METRIC_SPECS[name].labels:
            duration_labels["outcome"] = outcome
        record_metric(name, time.perf_counter() - started_at, **duration_labels)
