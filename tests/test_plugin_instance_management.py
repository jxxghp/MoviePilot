"""插件实例增删查管理器方法的行为契约测试。

覆盖列表返回全部实例及运行状态、创建实例写入配置并拉起（含首次创建时固化
默认实例行）、删除实例回收配置数据与自管理库及持久化目录、路径穿越标识在
任何写入之前被拒绝、默认实例不可删除、以及针对不存在插件的错误处理。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.db.models.plugindata import PluginData
from app.db.models.pluginconfig import PluginConfig
from app.db.oper.pluginconfig import PluginConfigOper
from app.db.oper.plugindata import PluginDataOper
from app.foundation.singleton import Singleton
from app.sdk.extension import _PluginBase
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID, instance_key
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.runtime.extensions.plugin_manager import PluginManager
from app.startup.plugins_initializer import (
    _delete_plugin_instance_config_row,
    _delete_plugin_instance_data_rows,
    _list_plugin_instance_ids,
    _read_plugin_instance_config,
    _upsert_plugin_instance_config_row,
    _write_plugin_instance_config,
)

PLUGIN_ID = "_InstanceMgmtPlugin"
SECOND_INSTANCE = "second"
SECOND_KEY = f"{PLUGIN_ID}@{SECOND_INSTANCE}"


class _InstanceMgmtPlugin(_PluginBase):
    """驱动实例创建与删除测试的最小插件。"""

    plugin_name = "实例管理演示"
    plugin_version = "1.0.0"

    def init_plugin(self, config: dict = None):
        """生效配置信息，测试桩不做额外处理。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool((self.get_config() or {}).get("enable"))

    def get_api(self) -> List[Dict[str, Any]]:
        """返回携带本实例标记的 API 声明。"""
        return [{"path": "/status", "endpoint": lambda: None, "methods": ["GET"]}]

    def get_service(self) -> List[Dict[str, Any]]:
        """返回携带本实例标记的定时服务声明。"""
        return [{"id": "sync", "name": "同步", "trigger": "interval", "func": lambda: None}]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单。"""
        return None, {}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页。"""
        return None

    def stop_service(self):
        """停止插件服务，测试桩无后台服务。"""


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture(autouse=True)
def _restore_plugin_database_ports() -> Iterator[None]:
    """快照并复原插件数据库生命周期端口，避免用例间相互污染。"""
    saved_ensure = plugin_manager_module._plugin_database_ensure
    saved_release = plugin_manager_module._plugin_database_release
    saved_destroy = plugin_manager_module._plugin_database_destroy
    saved_config_upsert = plugin_manager_module._plugin_instance_config_upsert
    saved_config_delete = plugin_manager_module._plugin_instance_config_delete
    saved_data_delete = plugin_manager_module._plugin_instance_data_delete
    yield
    plugin_manager_module._plugin_database_ensure = saved_ensure
    plugin_manager_module._plugin_database_release = saved_release
    plugin_manager_module._plugin_database_destroy = saved_destroy
    plugin_manager_module._plugin_instance_config_upsert = saved_config_upsert
    plugin_manager_module._plugin_instance_config_delete = saved_config_delete
    plugin_manager_module._plugin_instance_data_delete = saved_data_delete


@pytest.fixture
def production_plugin_config_storage() -> Iterator[None]:
    """按启动组合根同款接线，把插件配置端口接到真实 PluginConfigOper。"""
    original = get_plugin_storage()
    configure_plugin_storage(PluginStorage(
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        list_instances=_list_plugin_instance_ids,
    ))
    plugin_manager_module._configure_plugin_instance_persistence(
        upsert_config=_upsert_plugin_instance_config_row,
        delete_config=_delete_plugin_instance_config_row,
        delete_data=_delete_plugin_instance_data_rows,
    )
    yield
    configure_plugin_storage(original)


@pytest.fixture
def plugin_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件数据根目录指向临时目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugin_paths_module, "settings", SimpleNamespace(PLUGIN_DATA_PATH=root))
    return root


def _install_plugin(monkeypatch: pytest.MonkeyPatch, manager: PluginManager) -> None:
    """把测试插件接入管理器的加载路径，数据库钩子替换为安全空操作。"""
    monkeypatch.setattr(
        manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_InstanceMgmtPlugin],
    )
    plugin_manager_module._plugin_database_ensure = lambda _pid, _iid: None
    plugin_manager_module._plugin_database_release = lambda _pid: None
    plugin_manager_module._plugin_database_destroy = lambda _pid, _iid: None


@pytest.fixture
def ready_plugin(monkeypatch, plugin_manager, db, plugin_data_root, production_plugin_config_storage):
    """安装测试插件并完成一次默认实例启动，返回可直接使用的管理器。"""
    db.watermark(PluginConfig)
    db.watermark(PluginData)
    _install_plugin(monkeypatch, plugin_manager)
    plugin_manager.start(pid=PLUGIN_ID)
    return plugin_manager


def test_list_plugin_instances_returns_running_status(ready_plugin):
    """列表返回该插件全部实例及其运行状态。"""
    PluginConfigOper().upsert(PLUGIN_ID, SECOND_INSTANCE, {"config_data": {"enable": True}})
    ready_plugin.start_instance(PLUGIN_ID, SECOND_INSTANCE)

    result = ready_plugin.list_plugin_instances(PLUGIN_ID)

    assert result == [
        {
            "instance_id": DEFAULT_INSTANCE_ID,
            "instance_key": PLUGIN_ID,
            "running": True,
            "state": False,
        },
        {
            "instance_id": SECOND_INSTANCE,
            "instance_key": SECOND_KEY,
            "running": True,
            "state": True,
        },
    ]


def test_list_plugin_instances_rejects_unknown_plugin(ready_plugin):
    """对不存在的插件列出实例时抛出 LookupError。"""
    with pytest.raises(LookupError):
        ready_plugin.list_plugin_instances("_NoSuchPlugin")


def test_create_plugin_instance_fixates_default_row_first_time(ready_plugin):
    """首次创建实例时，此前没有任何配置记录的默认实例先被固化成一行。"""
    assert PluginConfigOper().list_by_plugin(PLUGIN_ID) == []

    ready_plugin.create_plugin_instance(PLUGIN_ID, SECOND_INSTANCE, {"enable": True})

    rows = {row.instance_id: row for row in PluginConfigOper().list_by_plugin(PLUGIN_ID)}
    assert set(rows) == {DEFAULT_INSTANCE_ID, SECOND_INSTANCE}
    assert rows[DEFAULT_INSTANCE_ID].config_data == {}


def test_create_plugin_instance_writes_config_and_starts(ready_plugin):
    """创建实例后：配置行写入、实例被拉起、该实例的服务与 API 声明出现。"""
    info = ready_plugin.create_plugin_instance(PLUGIN_ID, SECOND_INSTANCE, {"enable": True})

    assert info == {
        "instance_id": SECOND_INSTANCE,
        "instance_key": SECOND_KEY,
        "running": True,
        "state": True,
    }
    row = PluginConfigOper().get(PLUGIN_ID, SECOND_INSTANCE)
    assert row is not None
    assert row.config_data == {"enable": True}
    assert SECOND_KEY in ready_plugin._running_plugins
    # 新实例的定时服务与 API 出现在插件投影中，这正是 register_plugin 会读取的数据源
    service_owners = {service.get("pid") for service in ready_plugin.get_plugin_services(PLUGIN_ID)}
    api_paths = {api.get("path") for api in ready_plugin.get_plugin_apis(PLUGIN_ID)}
    assert SECOND_KEY in service_owners
    assert f"/{SECOND_KEY}/status" in api_paths


def test_create_plugin_instance_rejects_unknown_plugin(ready_plugin):
    """对不存在的插件创建实例时抛出 LookupError。"""
    with pytest.raises(LookupError):
        ready_plugin.create_plugin_instance("_NoSuchPlugin", SECOND_INSTANCE)


def test_create_plugin_instance_rejects_duplicate(ready_plugin):
    """已存在的实例标识不能重复创建。"""
    ready_plugin.create_plugin_instance(PLUGIN_ID, SECOND_INSTANCE)

    with pytest.raises(ValueError):
        ready_plugin.create_plugin_instance(PLUGIN_ID, SECOND_INSTANCE)


def test_create_plugin_instance_rejects_path_traversal_before_any_write(ready_plugin):
    """实例标识为路径穿越时被拒绝，且拒绝发生在任何文件或数据库写入之前。"""
    with pytest.raises(ValueError):
        ready_plugin.create_plugin_instance(PLUGIN_ID, "../../etc")

    assert PluginConfigOper().list_by_plugin(PLUGIN_ID) == []
    assert instance_key(PLUGIN_ID, "../../etc") not in ready_plugin._running_plugins


def test_delete_plugin_instance_recycles_everything_and_keeps_sibling(
    ready_plugin, plugin_data_root
):
    """
    删除实例后：运行态停止、配置与数据行消失、数据目录被回收、
    该实例的服务与 API 声明消失，且兄弟实例的登记完好。
    """
    ready_plugin.create_plugin_instance(PLUGIN_ID, SECOND_INSTANCE, {"enable": True})
    # 默认实例也启用，才能在服务投影里验证兄弟实例登记完好（get_service 只统计已启用实例）
    PluginConfigOper().upsert(PLUGIN_ID, DEFAULT_INSTANCE_ID, {"config_data": {"enable": True}})
    PluginDataOper().save(PLUGIN_ID, "cursor", 1, instance_id=SECOND_INSTANCE)
    PluginDataOper().save(PLUGIN_ID, "cursor", 2, instance_id=DEFAULT_INSTANCE_ID)
    # 模拟插件实际使用过数据目录，落出该实例的持久化目录
    data_path = ready_plugin._running_plugins[SECOND_KEY].get_data_path()
    instance_dir = data_path.parent
    assert instance_dir == plugin_data_root / PLUGIN_ID / SECOND_INSTANCE
    assert instance_dir.exists()

    destroyed: list = []
    plugin_manager_module._plugin_database_destroy = lambda pid, iid: destroyed.append((pid, iid))

    ready_plugin.delete_plugin_instance(PLUGIN_ID, SECOND_INSTANCE)

    assert SECOND_KEY not in ready_plugin._running_plugins
    assert PluginConfigOper().get(PLUGIN_ID, SECOND_INSTANCE) is None
    assert PluginDataOper().get_data(PLUGIN_ID, "cursor", instance_id=SECOND_INSTANCE) is None
    assert destroyed == [(PLUGIN_ID, SECOND_INSTANCE)]
    assert not instance_dir.exists()
    # 兄弟（默认）实例的登记完好：仍在运行、配置与数据仍在、服务与 API 仍存在
    assert PLUGIN_ID in ready_plugin._running_plugins
    assert PluginConfigOper().get(PLUGIN_ID, DEFAULT_INSTANCE_ID) is not None
    assert PluginDataOper().get_data(PLUGIN_ID, "cursor", instance_id=DEFAULT_INSTANCE_ID) == 2
    service_owners = {service.get("pid") for service in ready_plugin.get_plugin_services(PLUGIN_ID)}
    api_paths = {api.get("path") for api in ready_plugin.get_plugin_apis(PLUGIN_ID)}
    assert PLUGIN_ID in service_owners
    assert SECOND_KEY not in service_owners
    assert f"/{PLUGIN_ID}/status" in api_paths
    assert f"/{SECOND_KEY}/status" not in api_paths


def test_delete_plugin_instance_rejects_default_instance(ready_plugin):
    """默认实例不允许删除。"""
    with pytest.raises(ValueError):
        ready_plugin.delete_plugin_instance(PLUGIN_ID, DEFAULT_INSTANCE_ID)

    assert PLUGIN_ID in ready_plugin._running_plugins


def test_delete_plugin_instance_rejects_unknown_plugin(ready_plugin):
    """对不存在的插件删除实例时抛出 LookupError。"""
    with pytest.raises(LookupError):
        ready_plugin.delete_plugin_instance("_NoSuchPlugin", SECOND_INSTANCE)


def test_delete_plugin_instance_rejects_unknown_instance(ready_plugin):
    """删除未登记的实例时抛出 LookupError。"""
    with pytest.raises(LookupError):
        ready_plugin.delete_plugin_instance(PLUGIN_ID, "never-created")
