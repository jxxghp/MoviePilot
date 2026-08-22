"""_PluginBase 插件自管理数据库钩子方法测试：declare_plugin_models /
declare_plugin_migrations / get_plugin_database 的委托行为。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.db.plugin import registry as plugin_registry_module
from app.db.plugin.base import plugin_declarative_base
from app.sdk.extension import _PluginBase
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


class _SamplePlugin(_PluginBase):
    """只实现抽象契约的最小插件，用于驱动新增的数据库钩子方法。"""

    def init_plugin(self, config: dict = None):
        """生效配置信息。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 声明。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单。"""
        return None, {}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页。"""
        return None

    def stop_service(self):
        """停止插件服务。"""


@pytest.fixture
def plugin_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件数据根目录指向临时目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugin_paths_module, "settings", SimpleNamespace(PLUGIN_DATA_PATH=root))
    return root


@pytest.fixture(autouse=True)
def _isolate_plugin_database_registry():
    """快照并复原插件数据库注册表的全局状态，避免用例间相互污染。"""
    saved_declarations = dict(plugin_registry_module._declarations)
    saved_containers = dict(plugin_registry_module._containers)
    plugin_registry_module._declarations.clear()
    plugin_registry_module._containers.clear()
    yield
    for key in list(plugin_registry_module._containers):
        plugin_registry_module.release_instance(*key)
    plugin_registry_module._declarations.clear()
    plugin_registry_module._declarations.update(saved_declarations)
    plugin_registry_module._containers.clear()
    plugin_registry_module._containers.update(saved_containers)


def test_declare_plugin_models_registers_under_class_name(plugin_data_root: Path):
    """未传插件ID时按插件类名登记声明，且注册表能读到同一个基类。"""
    base = plugin_declarative_base("_SamplePlugin")
    plugin = _SamplePlugin()

    plugin.declare_plugin_models(base)

    key = ("_SamplePlugin", DEFAULT_INSTANCE_ID)
    assert plugin_registry_module._declarations[key].base is base


def test_declare_plugin_migrations_converts_to_path(plugin_data_root: Path):
    """声明迁移目录时接受字符串路径，登记后转换为 Path。"""
    plugin = _SamplePlugin()

    plugin.declare_plugin_migrations("some/migrations/dir")

    key = ("_SamplePlugin", DEFAULT_INSTANCE_ID)
    declared = plugin_registry_module._declarations[key].migrations_dir
    assert declared == Path("some/migrations/dir")


def test_get_plugin_database_returns_handle_for_current_plugin(plugin_data_root: Path):
    """未传插件ID时取当前插件的数据库句柄，库文件落在插件自己的 db 目录下。"""
    plugin = _SamplePlugin()

    handle = plugin.get_plugin_database()

    assert handle.plugin_id == "_SamplePlugin"
    assert handle.instance_id == DEFAULT_INSTANCE_ID
    assert handle.db_path.parent == plugin_data_root / "_SamplePlugin" / "default" / "db"


def test_get_plugin_database_honours_explicit_plugin_id(plugin_data_root: Path):
    """显式传入插件ID时应覆盖默认的类名推断。"""
    plugin = _SamplePlugin()

    handle = plugin.get_plugin_database(plugin_id="OtherPlugin")

    assert handle.plugin_id == "OtherPlugin"
