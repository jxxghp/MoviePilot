"""
可选的 OpenTelemetry 指标适配器，将运行时指标发送给已安装的指标提供器。
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Mapping

from app.runtime.observability import MetricKind, MetricSpec, NoopObservationPort, ObservationPort


class OpenTelemetryObservationPort:
    """
    把内部指标合同映射到可选安装的 OpenTelemetry Metrics API。
    """

    def __init__(self, meter: Any) -> None:
        """
        保存指标提供器，并按指标名称延迟创建采集器。

        :param meter: OpenTelemetry 的指标提供器，由调用方负责初始化和关闭
        """
        self._meter = meter
        self._instruments: dict[str, Any] = {}

    def record(self, spec: MetricSpec, value: float, labels: Mapping[str, str]) -> None:
        """
        按指标类型记录观测值，复用同名指标的采集器。

        :param spec: 指标名称与类型契约；同名指标应保持同一类型
        :param value: 本次观测值；直方图记录样本，计数器累加增量
        :param labels: 本次观测的标签，复制后交给 OpenTelemetry
        """
        instrument = self._instruments.get(spec.name)
        if instrument is None:
            instrument = self._create_instrument(spec)
            self._instruments[spec.name] = instrument
        if spec.kind == MetricKind.HISTOGRAM:
            instrument.record(value, attributes=dict(labels))
        else:
            instrument.add(value, attributes=dict(labels))

    def _create_instrument(self, spec: MetricSpec) -> Any:
        """
        创建直方图、单调计数器或可增减计数器，名称中的点转换为下划线。
        """
        normalized = spec.name.replace(".", "_")
        if spec.kind == MetricKind.HISTOGRAM:
            return self._meter.create_histogram(normalized)
        if spec.kind == MetricKind.COUNTER:
            return self._meter.create_counter(normalized)
        return self._meter.create_up_down_counter(normalized)


def build_observation_port() -> ObservationPort:
    """
    按环境开关构造指标适配器。

    :return: MOVIEPILOT_OTEL_METRICS 为 1 且可导入 OpenTelemetry 时返回适配器，
        否则返回忽略指标的空实现
    """
    if os.getenv("MOVIEPILOT_OTEL_METRICS") != "1":
        return NoopObservationPort()
    try:
        metrics = importlib.import_module("opentelemetry.metrics")
    except ImportError:
        return NoopObservationPort()
    return OpenTelemetryObservationPort(metrics.get_meter("moviepilot"))
