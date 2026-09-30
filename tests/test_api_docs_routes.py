"""API 文档运行时开关的路由契约测试。"""

import pytest
from fastapi.testclient import TestClient

from app.application.plugin import routes as plugin_routes
from app.application.settings.contract import build_setting_specs
from app.factory import create_app
from app.runtime.config import settings

DOC_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/api/v1/openapi.json")


def test_api_docs_are_closed_by_default() -> None:
    """默认不开放文档，也不会因访问而生成文档。"""
    app = create_app()
    client = TestClient(app)

    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path
    assert app.openapi_schema is None


def test_api_docs_follow_runtime_toggle_without_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个应用实例上切换开关即时生效，关闭后丢弃已缓存的文档。"""
    app = create_app()
    client = TestClient(app)
    monkeypatch.setattr(settings, "API_DOCS_ENABLE", True)

    openapi = client.get("/api/v1/openapi.json")
    assert openapi.status_code == 200
    assert openapi.json()["openapi"].startswith("3.")
    assert app.openapi_schema is not None
    swagger = client.get("/docs")
    assert swagger.status_code == 200
    assert "/api/v1/openapi.json" in swagger.text
    assert "/docs/oauth2-redirect" in swagger.text
    assert client.get("/docs/oauth2-redirect").status_code == 200
    redoc = client.get("/redoc")
    assert redoc.status_code == 200
    assert "/api/v1/openapi.json" in redoc.text

    monkeypatch.setattr(settings, "API_DOCS_ENABLE", False)
    assert client.get("/api/v1/openapi.json").status_code == 404
    assert app.openapi_schema is None


def test_api_docs_survive_plugin_route_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件路由增删后文档路由仍在，FastAPI.setup() 不再补注册它们。"""
    app = create_app()
    registry = plugin_routes._get_route_registry()
    # 测试进程没有运行中的主 loop，改为当前线程同步更新路由表。
    monkeypatch.setattr(registry, "_event_loop", None)
    monkeypatch.setattr(registry, "_plugin_apis", lambda _plugin_id: [{
        "path": "/DemoPlugin/health",
        "endpoint": lambda: {"ok": True},
        "methods": ["GET"],
        "allow_anonymous": True,
    }])
    monkeypatch.setattr(settings, "API_DOCS_ENABLE", True)
    client = TestClient(app)

    registry.update("DemoPlugin", "add")
    assert client.get("/api/v1/plugin/DemoPlugin/health").json() == {"ok": True}
    registry.update("DemoPlugin", "remove")

    for path in DOC_PATHS:
        assert client.get(path).status_code == 200, path


def test_api_docs_pages_keep_reverse_proxy_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """文档页引用的 OpenAPI 地址带上 root_path，与 FastAPI 自带文档一致。"""
    monkeypatch.setattr(settings, "API_DOCS_ENABLE", True)
    client = TestClient(create_app(), root_path="/mp")

    assert "/mp/api/v1/openapi.json" in client.get("/docs").text
    assert "/mp/api/v1/openapi.json" in client.get("/redoc").text


def test_api_docs_toggle_applies_immediately() -> None:
    """开关位于需重启的安全分组，但逐请求读取，设置契约应标记为立即生效。"""
    core_specs, _ = build_setting_specs()
    spec = core_specs["API_DOCS_ENABLE"]

    assert spec.group == "security"
    assert spec.apply_mode == "immediate"
    assert core_specs["PASSKEY_REQUIRE_UV"].apply_mode == "restart_required"
