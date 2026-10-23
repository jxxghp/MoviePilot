"""
验证动态插件认证、路由卸载与文件夹JSON持久化边界。
"""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.api.endpoints import pluginfolder
from app.application.plugin.folders import PluginFolderService
from app.schemas.plugin import PluginFoldersData


def test_existing_bearer_dependency_does_not_also_require_apikey():
    """
    插件已声明同一bearer依赖时，认证去重不能意外增加API密钥要求。
    """
    app = FastAPI()

    def bearer(authorization: str = Header("")):
        """
        只接受本测试的合成Bearer凭据。
        """
        if authorization != "Bearer synthetic":
            raise HTTPException(401)

    def apikey(x_api_key: str = Header("")):
        """
        API密钥入口保持独立，缺失时拒绝。
        """
        if x_api_key != "synthetic-key":
            raise HTTPException(403)

    def endpoint():
        """
        返回可观察的插件业务响应。
        """
        return {"ok": True}

    api = {"path": "/Demo", "endpoint": endpoint, "methods": ["GET"],
           "auth": "bear", "dependencies": [Depends(bearer)]}
    registry = FastAPIDynamicRouteRegistry(
        app=app, plugin_ids=lambda: ["Demo"], plugin_apis=lambda _: [api],
        verify_token=bearer, verify_apikey=apikey, prefix="/plugin", log=Mock(),
    )
    registry.update("Demo", "add")
    with TestClient(app) as client:
        assert client.get("/plugin/Demo", headers={"Authorization": "Bearer synthetic"}).status_code == 200
        assert client.get("/plugin/Demo").status_code == 401
    assert len(api["dependencies"]) == 1


def test_unloading_root_and_children_preserves_other_plugin_prefix():
    """
    卸载同时删除插件根与子路由，不误删共享文本前缀的其他插件。
    """
    app = FastAPI()

    def endpoint():
        """
        提供内存路由的成功响应。
        """
        return {"ok": True}

    registry = FastAPIDynamicRouteRegistry(
        app=app, plugin_ids=lambda: ["Demo"],
        plugin_apis=lambda _: [
            {"path": path, "endpoint": endpoint, "methods": ["GET"], "allow_anonymous": True}
            for path in ("/Demo", "/Demo/child", "/DemoOther")
        ],
        verify_token=lambda: None, verify_apikey=lambda: None,
        prefix="/plugin", log=Mock(),
    )
    registry.update("Demo", "add")
    registry.update("Demo", "remove")
    with TestClient(app) as client:
        assert client.get("/plugin/Demo").status_code == 404
        assert client.get("/plugin/Demo/child").status_code == 404
        assert client.get("/plugin/DemoOther").status_code == 200


@pytest.mark.asyncio
async def test_folder_endpoint_persists_objects_and_legacy_lists_as_json(tmp_path, monkeypatch):
    """
    实际JSON列必须接收可序列化对象，保存后保留showIcon别名与旧列表格式。
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'folders.db'}")
    table = sa.Table("folder_config", sa.MetaData(), sa.Column("value", sa.JSON))
    table.metadata.create_all(engine)

    async def write(folders):
        """
        在真实临时SQLite事务中触发JSON序列化和持久化。
        """
        with engine.begin() as connection:
            connection.execute(table.insert().values(value=folders))

    service = PluginFolderService(
        read=dict, write=write, write_sync=lambda _: None,
        mutation=lambda _: nullcontext(),
    )
    monkeypatch.setattr(pluginfolder, "get_plugin_folder_service", lambda: service)
    payload = PluginFoldersData.model_validate({
        "object": {"plugins": ["Demo"], "showIcon": True, "color": "red"},
        "legacy": ["Other"],
    })
    try:
        result = await pluginfolder.save_plugin_folders(payload, None)
        assert result.success
        with engine.connect() as connection:
            saved = connection.execute(sa.select(table.c.value)).scalar_one()
        assert saved["object"]["showIcon"] is True
        assert "show_icon" not in saved["object"]
        assert saved["object"]["plugins"] == ["Demo"]
        assert saved["legacy"] == ["Other"]
    finally:
        engine.dispose()
