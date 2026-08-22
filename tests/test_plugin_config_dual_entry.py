"""插件配置的两条读写入口——PluginManager 与 _PluginBase——必须互相可读。

插件配置存在两条历史入口：``PluginManager.get_plugin_config``/``save_plugin_config``
经 ``PluginStorage`` 端口，``_PluginBase.get_config``/``update_config`` 直接使用
``PluginConfigOper``。两条入口分别改走插件实例配置表后，一条写入必须能被另一条
读到——漏改其中一条会表现为「插件自己写的配置，管理器读不到」，且不会抛异常，
只会在下一次读取时安静地拿到旧值或空值。
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.db.models.pluginconfig import PluginConfig
from app.sdk.extension import _PluginBase
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.startup.plugins_initializer import (
    _async_write_plugin_instance_config,
    _delete_plugin_instance_config,
    _read_plugin_instance_config,
    _write_plugin_instance_config,
)


class _DemoPlugin(_PluginBase):
    """只实现抽象契约的最小插件，用于驱动配置读写。"""

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


@pytest.fixture(autouse=True)
def _track(db):
    """把插件实例配置表纳入用例级回收。"""
    db.watermark(PluginConfig)


@pytest.fixture
def production_plugin_config_storage():
    """按启动组合根同款接线，把插件配置端口接到真实 PluginConfigOper。"""
    original = get_plugin_storage()
    configure_plugin_storage(PluginStorage(
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        async_write_config=_async_write_plugin_instance_config,
        delete_config=_delete_plugin_instance_config,
    ))
    yield
    configure_plugin_storage(original)


def test_manager_write_is_visible_to_plugin_base_read(
    production_plugin_config_storage, monkeypatch
):
    """PluginManager 保存的配置，_PluginBase 必须能读到。"""
    manager = plugin_manager_module.PluginManager()
    monkeypatch.setattr(manager, "_plugins", {"_DualEntryManagerWrite": object()})

    manager.save_plugin_config("_DualEntryManagerWrite", {"enable": True, "cron": "5 4 * * *"})

    plugin = _DemoPlugin()
    assert plugin.get_config("_DualEntryManagerWrite") == {
        "enable": True,
        "cron": "5 4 * * *",
    }


def test_plugin_base_write_is_visible_to_manager_read(
    production_plugin_config_storage, monkeypatch
):
    """_PluginBase 保存的配置，PluginManager 必须能读到。"""
    plugin = _DemoPlugin()
    plugin.update_config({"threshold": 5}, "_DualEntryBaseWrite")

    manager = plugin_manager_module.PluginManager()
    monkeypatch.setattr(manager, "_plugins", {"_DualEntryBaseWrite": object()})

    assert manager.get_plugin_config("_DualEntryBaseWrite") == {"threshold": 5}


def test_manager_delete_is_visible_to_plugin_base_read(
    production_plugin_config_storage, monkeypatch
):
    """PluginManager 删除配置后，_PluginBase 必须读到空。"""
    manager = plugin_manager_module.PluginManager()
    monkeypatch.setattr(manager, "_plugins", {"_DualEntryDelete": object()})
    manager.save_plugin_config("_DualEntryDelete", {"enable": True})

    assert manager.delete_plugin_config("_DualEntryDelete", force=True) is True

    plugin = _DemoPlugin()
    assert plugin.get_config("_DualEntryDelete") is None
