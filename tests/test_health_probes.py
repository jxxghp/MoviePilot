"""公开 liveness/readiness 探针契约测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.factory import create_app
from app.runtime.health import ReadinessPhase, get_application_health


PROJECT_ROOT = Path(__file__).parents[1]


def test_liveness_is_public_minimal_and_independent_from_readiness() -> None:
    """存活探针不访问依赖，即使启动失败也只报告事件循环仍可响应。"""
    app = create_app()
    get_application_health(app).mark_failed()

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_is_unavailable_before_database_and_lifecycle_complete() -> None:
    """尚未进入 lifespan 或启动失败时不得让编排器接入流量。"""
    app = create_app()
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_only_exposes_ready_after_database_and_lifecycle() -> None:
    """数据库 head 校验和完整启动都成功后才返回最小 ready 状态。"""
    app = create_app()
    health = get_application_health(app)
    health.mark_database_ready()
    health.mark_ready()

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}

    health.mark_stopping()
    stopped = TestClient(app).get("/health/ready")
    assert stopped.status_code == 503
    assert stopped.json() == {"status": "not_ready"}
    assert health.phase is ReadinessPhase.STOPPING


def test_health_routes_are_public_but_hidden_from_business_openapi() -> None:
    """基础设施探针无需鉴权，但不扩大业务 OpenAPI 的公开契约。"""
    app = create_app()
    client = TestClient(app)

    assert client.get("/health/live").status_code == 200
    schema = client.get("/api/v1/openapi.json").json()
    assert "/health/live" not in schema["paths"]
    assert "/health/ready" not in schema["paths"]


def test_operational_consumers_use_readiness_probe() -> None:
    """容器、本地前端和性能工具不得再借业务接口判断启动完成。"""
    paths = (
        "docker/Dockerfile",
        "docker/entrypoint.sh",
        "scripts/local_setup.py",
        "scripts/perf/moviepilot_docker_ab.py",
    )
    contents = [
        (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in paths
    ]

    assert all("/health/ready" in content for content in contents)
    assert all(
        "system/global?token=moviepilot" not in content
        for content in contents
    )
