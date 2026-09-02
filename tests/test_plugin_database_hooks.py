"""_PluginBase 自有数据库钩子与插件 SDK 数据库入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column

import app.db.plugin.locator as locator_module
import app.db.plugin.registry as registry_module
from app.db.decorators import db_query, db_update
from app.db.plugin.base import plugin_declarative_base
from app.plugins import _PluginBase


class _SamplePlugin(_PluginBase):
    """只实现最小生命周期合同的插件测试替身。"""

    plugin_name = "示例插件"

    def init_plugin(self, config: dict = None):
        """接受宿主初始化配置。"""

    def get_state(self) -> bool:
        """保持插件为启用状态。"""
        return True

    def get_api(self) -> List[Dict[str, Any]]:
        """不注册任何 API。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """不提供配置页面。"""
        return None, {}

    def get_page(self) -> Optional[List[dict]]:
        """不提供详情页面。"""
        return None

    def stop_service(self):
        """无后台资源需要停止。"""


@pytest.fixture(autouse=True)
def _isolate_plugin_databases():
    """快照插件数据库句柄，用例结束后释放残留句柄并还原快照。"""
    handles = dict(registry_module._handles)
    registry_module._handles.clear()
    yield
    for plugin_id in list(registry_module._handles):
        registry_module.release_database(plugin_id)
    registry_module._handles.clear()
    registry_module._handles.update(handles)


@pytest.fixture
def plugin_data_root(tmp_path, monkeypatch) -> Path:
    """把插件数据库文件隔离到进程私有的临时目录。"""
    root = tmp_path / "plugins"
    monkeypatch.setattr(
        locator_module,
        "get_runtime_setting",
        lambda key, default=None: root if key == "PLUGIN_DATA_PATH" else default,
    )
    return root


def test_database_declaration_hooks_default_to_no_declaration():
    """未重写声明钩子的插件视为既未声明模型也未声明迁移目录。"""
    plugin = _SamplePlugin()
    assert plugin.get_database_models() is None
    assert plugin.get_database_migrations() is None


def test_get_database_uses_the_plugin_class_name(plugin_data_root):
    """未显式传入插件ID时，句柄按插件类名解析，与 get_data_path() 目录约定一致。"""
    plugin = _SamplePlugin()

    handle = plugin.get_database()

    assert handle.plugin_id == "_SamplePlugin"
    assert handle.db_path == plugin_data_root / "_SamplePlugin" / "plugin.db"


def test_get_database_honours_an_explicit_plugin_id(plugin_data_root):
    """显式传入插件ID时按该ID解析句柄，不回退到类名。"""
    plugin = _SamplePlugin()

    handle = plugin.get_database(plugin_id="OtherPlugin")

    assert handle.plugin_id == "OtherPlugin"


def test_clone_class_name_selects_its_own_database(plugin_data_root):
    """分身的类名即插件运行时标识，句柄与源插件互不相同。"""
    clone_cls = type("_SamplePluginwork", (_SamplePlugin,), {})
    clone = clone_cls()
    source = _SamplePlugin()

    clone_handle = clone.get_database()
    source_handle = source.get_database()

    assert clone_handle.plugin_id == "_SamplePluginwork"
    assert clone_handle.db_path != source_handle.db_path


def test_declared_models_reach_the_framework_through_the_lifecycle_hook(plugin_data_root):
    """宿主一次性拉取 get_database_models() 的返回值建表，端到端验证拉取式链路。"""
    base = plugin_declarative_base()

    class Widget(base):
        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)

    class _ModelPlugin(_SamplePlugin):
        """声明一个模型的插件测试替身。"""

        def get_database_models(self):
            """声明 Widget 模型。"""
            return [Widget]

    plugin = _ModelPlugin()
    registry_module.ensure_database("_ModelPlugin", plugin.get_database_models())

    handle = plugin.get_database()
    tables = set(sa_inspect(handle.engine).get_table_names())
    assert tables == {"widgets"}


def test_sdk_database_exports_the_plugin_database_contract():
    """SDK 门面复用 db 层的同一对象，不复制实现或制造第二套定义。"""
    import app.db.plugin.base as base_module
    import app.db.plugin.container as container_module
    import app.sdk.database as sdk_database

    assert sdk_database.plugin_declarative_base is base_module.plugin_declarative_base
    assert sdk_database.PluginDatabaseHandle is container_module.PluginDatabaseHandle
    assert "plugin_declarative_base" in sdk_database.__all__
    assert "PluginDatabaseHandle" in sdk_database.__all__


def test_host_transaction_decorators_accept_a_plugin_owned_session(plugin_data_root):
    """db_update/db_query 的自动兜底不认识插件库，但显式传入的插件会话原样可用。"""
    base = plugin_declarative_base()

    class Widget(base):
        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column()

    registry_module.ensure_database("demo", (Widget,))
    handle = registry_module.get_database("demo")

    @db_update
    def _write(db, widget):
        """插入一条记录，提交由装饰器负责。"""
        db.add(widget)

    @db_query
    def _count(db):
        """读取记录数。"""
        return db.query(Widget).count()

    session = handle.session()
    try:
        _write(session, Widget(id=1, name="a"))
        assert _count(session) == 1
    finally:
        session.close()
