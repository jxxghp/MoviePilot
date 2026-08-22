"""可选 OpenTelemetry metrics adapter。"""

from __future__ import annotations

import importlib
import os
from typing import Any, Mapping

from app.runtime.observability import MetricKind, MetricSpec, NoopObservationPort, ObservationPort


class OpenTelemetryObservationPort:
    """把内部指标合同映射到可选安装的 OpenTelemetry Metrics API。"""

    def __init__(self, meter: Any) -> None:
        """保存 meter，并按名称惰性创建 instrument。"""
        self._meter = meter
        self._instruments: dict[str, Any] = {}

    def record(self, spec: MetricSpec, value: float, labels: Mapping[str, str]) -> None:
        """按合同类型使用 OTel counter、histogram 或 up/down counter。"""
        instrument = self._instruments.get(spec.name)
        if instrument is None:
            instrument = self._create_instrument(spec)
            self._instruments[spec.name] = instrument
        if spec.kind == MetricKind.HISTOGRAM:
            instrument.record(value, attributes=dict(labels))
        else:
            instrument.add(value, attributes=dict(labels))

    def _create_instrument(self, spec: MetricSpec) -> Any:
        """为内部指标类型创建对应 OTel instrument。"""
        normalized = spec.name.replace(".", "_")
        if spec.kind == MetricKind.HISTOGRAM:
            return self._meter.create_histogram(normalized)
        if spec.kind == MetricKind.COUNTER:
            return self._meter.create_counter(normalized)
        return self._meter.create_up_down_counter(normalized)


def build_observation_port() -> ObservationPort:
    """仅在显式启用且 API 可导入时创建 OTel adapter，否则返回 no-op。"""
    if os.getenv("MOVIEPILOT_OTEL_METRICS") != "1":
        return NoopObservationPort()
    try:
        metrics = importlib.import_module("opentelemetry.metrics")
    except ImportError:
        return NoopObservationPort()
    return OpenTelemetryObservationPort(metrics.get_meter("moviepilot"))
