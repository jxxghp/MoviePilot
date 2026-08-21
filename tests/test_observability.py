"""低基数指标合同、no-op 与 HTTP adapter 测试。"""

from dataclasses import dataclass, field
from typing import Mapping

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.adapters.observability import otel
from app.adapters.web.metrics import HttpMetricsMiddleware
from app.runtime.observability import (
    METRIC_SPECS,
    MetricSpec,
    NoopObservationPort,
    configure_observation,
    observe_duration,
    record_metric,
)


@dataclass
class RecordingObservationPort:
    """测试用端口，保存已经通过核心标签校验的写入。"""

    records: list[tuple[MetricSpec, float, Mapping[str, str]]] = field(
        default_factory=list
    )

    def record(
        self, spec: MetricSpec, value: float, labels: Mapping[str, str]
    ) -> None:
        """追加一条不可变测试快照。"""
        self.records.append((spec, value, dict(labels)))


@pytest.fixture(autouse=True)
def _reset_observation_port():
    """避免进程级观测端口在用例间泄漏。"""
    configure_observation(None)
    yield
    configure_observation(None)


def test_noop_port_accepts_registered_metric_without_exporter() -> None:
    """未安装 exporter 时记录合法指标必须完全可用。"""
    configure_observation(NoopObservationPort())
    record_metric(
        "http.server.duration",
        0.1,
        route="/health/live",
        method="GET",
        status="200",
    )


def test_metric_catalog_contains_no_high_cardinality_labels() -> None:
    """整个指标目录不得登记用户、插件、媒体、URL 或请求实例标签。"""
    forbidden = {
        "user_id",
        "plugin_id",
        "media_id",
        "media_title",
        "url",
        "request_id",
        "job_id",
    }

    assert METRIC_SPECS
    assert all(not (spec.labels & forbidden) for spec in METRIC_SPECS.values())


def test_unregistered_label_is_rejected_before_adapter() -> None:
    """调用方不能绕过目录向 exporter 注入高基数标签。"""
    with pytest.raises(ValueError, match="未登记标签"):
        record_metric(
            "scheduler.job.duration",
            1,
            owner="plugin",
            outcome="success",
            job_id="dynamic-123",
        )


def test_duration_records_success_and_error_outcomes() -> None:
    """统一计时器把正常和异常路径收敛为有限 outcome。"""
    port = RecordingObservationPort()
    configure_observation(port)

    with observe_duration(
        "module.provider.duration", method="recognize_media", provider_type="system"
    ):
        pass
    with pytest.raises(RuntimeError):
        with observe_duration(
            "module.provider.duration",
            method="recognize_media",
            provider_type="plugin",
        ):
            raise RuntimeError("failed")

    assert [record[2]["outcome"] for record in port.records] == ["success", "error"]


@pytest.mark.asyncio
async def test_http_metrics_use_route_template_not_request_url() -> None:
    """HTTP 指标使用路由模板，不能把具体资源 ID 变成 label。"""
    port = RecordingObservationPort()
    configure_observation(port)

    async def item(_request):
        """返回固定测试响应。"""
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/items/{item_id}", item)])
    app.add_middleware(HttpMetricsMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/items/secret-item")

    assert response.status_code == 200
    _, _, labels = port.records[-1]
    assert labels == {"route": "/items/{item_id}", "method": "GET", "status": "200"}


def test_optional_otel_adapter_falls_back_to_noop(monkeypatch) -> None:
    """显式启用但未安装 OTel API 时启动仍返回 no-op。"""
    monkeypatch.setenv("MOVIEPILOT_OTEL_METRICS", "1")

    def missing(_name: str):
        """模拟可选依赖不存在。"""
        raise ImportError("missing")

    monkeypatch.setattr(otel.importlib, "import_module", missing)

    assert isinstance(otel.build_observation_port(), NoopObservationPort)
