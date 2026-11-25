"""
历史非法实例残留清理的数据库与 API 装配回归。
"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, null
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import plugin as plugin_dependencies
from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints import plugininstance as plugin_endpoint
from app.db.models.plugininstance import PluginInstance
from app.db.oper.plugininstance import PluginInstanceOper
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)

INVALID_ID = "Emby角色中文化"


@pytest.fixture(name="sessions")
def fixture_sessions():
    """
    独立内存库跨 HTTP 测试工作线程共享，不触碰宿主配置或真实数据库。
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    PluginInstance.__table__.create(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def _insert(sessions, **overrides) -> None:
    """
    写入与报告一致的历史空本体，允许用例覆盖需保护的列。
    """
    fields = {
        "instance_id": INVALID_ID,
        "source_plugin_id": INVALID_ID,
        "created_at": "2026-10-17",
        "updated_at": "2026-10-17",
        **overrides,
    }
    with sessions.begin() as session:
        session.add(PluginInstance(**fields))


def _purge(sessions, instance_id: str) -> bool:
    """
    在独立测试事务中调用现有实例 Oper 的历史清理端口。
    """
    with sessions.begin() as session:
        return PluginInstanceOper(session).purge_invalid_empty_host(instance_id)


@pytest.mark.parametrize("config_data", [None, null()])
def test_purge_removes_only_empty_invalid_host_and_is_repeatable(sessions, config_data) -> None:
    """
    SQL NULL 与 JSON null 均能回收，正常插件和不存在的记录不受影响。
    """
    _insert(sessions, config_data=config_data, plugin_name="", log_level="")
    _insert(sessions, instance_id="Valid_Plugin", source_plugin_id="Valid_Plugin")
    assert _purge(sessions, INVALID_ID)
    assert not _purge(sessions, INVALID_ID)
    assert not _purge(sessions, "Valid_Plugin")
    with sessions() as session:
        assert PluginInstance.get_by_instance_id(session, INVALID_ID) is None
        assert PluginInstance.get_by_instance_id(session, "Valid_Plugin") is not None


@pytest.mark.parametrize("settings", [
    {"config_data": {}},
    {"config_data": {"token": "preserved"}},
    {"config_data": False},
    {"is_enabled": True},
    {"is_default_target": True},
    {"log_level": "DEBUG"},
    {"log_expires_at": "2026-10-18"},
    {"plugin_name": "旧插件"},
    {"plugin_desc": "旧描述"},
    {"plugin_icon": "icon.png"},
    {"source_plugin_id": "SourcePlugin"},
])
def test_purge_preserves_every_setting_and_clone_identity(sessions, settings) -> None:
    """
    每个设置列和分身身份都构成保留理由，空配置对象也不能误删。
    """
    _insert(sessions, **settings)
    assert not _purge(sessions, INVALID_ID)
    with sessions() as session:
        record = PluginInstance.get_by_instance_id(session, INVALID_ID)
        assert record is not None
        for field, value in settings.items():
            assert getattr(record, field) == value


def test_purge_uses_bound_database_identity(sessions) -> None:
    """
    含 SQL 和路径字符的旧 ID 仅作为数据库绑定值，不能扩大删除范围。
    """
    malicious_id = "../'; DELETE FROM plugininstance; --"
    _insert(sessions)
    _insert(sessions, instance_id=malicious_id, source_plugin_id=malicious_id)
    assert _purge(sessions, malicious_id)
    with sessions() as session:
        assert PluginInstance.get_by_instance_id(session, INVALID_ID) is not None


def test_http_purge_routes_invalid_empty_host_to_database_only(sessions, monkeypatch) -> None:
    """
    真实依赖工厂与 Manager 端口能清掉中文残留，且不进入任何运行时删除操作。
    """
    _insert(sessions)
    def purge(instance_id: str) -> bool:
        """
        为 HTTP 测试打开一个独立事务并调用实例 Oper。
        """
        return _purge(sessions, instance_id)

    directory = PluginInstanceDirectory(purge_invalid_empty_host=purge)
    manager = object.__new__(PluginManager)
    manager._plugin_instance_store = PluginInstanceStore(
        storage=PluginStorage, directory=lambda: directory,
    )
    manager.mutation = lambda _operation: nullcontext()
    stop = MagicMock(side_effect=AssertionError("不得停止历史无效实例"))
    manager.stop = stop
    monkeypatch.setattr(plugin_dependencies, "get_plugin_manager", lambda: manager)
    app = FastAPI()
    app.include_router(plugin_endpoint.router, prefix="/api/v1/plugin")
    app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(is_superuser=True)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/plugin/instance/{quote(INVALID_ID)}/purge", json={"config": True},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"purged": ["config"], "instance_removed": True}
    stop.assert_not_called()
    with sessions() as session:
        assert PluginInstance.get_by_instance_id(session, INVALID_ID) is None
