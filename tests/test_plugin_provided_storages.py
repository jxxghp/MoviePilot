"""插件声明存储类型链路测试：契约校验、登记归属、停用回收与内建取值还原。

存储类型与三族同走 `provides_service_instances()`，``capability="storage"``；差别只在
构造协议——``impl`` 是按令牌取用的存储后端类，构造走工厂（宿主默认那一个或声明自带的
那一个）。本文件同时盯住两张表：存储后端注册表按令牌取用，服务实例类型目录按类型记账。
"""

from typing import Iterator, List, Optional

import pytest

from app.application.storage import StorageHelper
from app.foundation.singleton import Singleton
from app.modules._base.storage import StorageBase
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.admission import extension_scoped, storage
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.registry.service_instance import (
    service_instance_registry,
)
from app.runtime.extensions.registry.storage import storage_backend_registry
from app.schemas.system import StorageConf


def _storage_declaration(storage_type, impl, **fields):
    """构造一条存储类型的服务实例声明。

    :param storage_type: 存储标识，同时是类型标识
    :param impl: 存储后端类
    :param fields: 声明的其余字段，``name`` 缺省时回落为存储标识
    :return: 服务实例声明
    """
    return ServiceInstanceDeclaration(
        capability="storage",
        type=storage_type,
        name=fields.pop("name", None) or storage_type or "演示存储",
        impl=impl,
        **fields,
    )


class _ValidPluginStorage(StorageBase):
    """契约合规的存储后端桩，全部抽象方法均已落地。"""

    def init_storage(self):
        """无需建立任何连接。"""

    def check(self) -> bool:
        """存储始终可用。"""
        return True

    def list(self, fileitem):
        """返回空列表。"""
        return []

    def create_folder(self, fileitem, name):
        """不提供实际创建。"""
        return None

    def get_folder(self, path):
        """不提供实际查询。"""
        return None

    def get_item(self, path):
        """不提供实际查询。"""
        return None

    def delete(self, fileitem):
        """删除始终成功。"""
        return True

    def rename(self, fileitem, name):
        """重命名始终成功。"""
        return True

    def download(self, fileitem, path=None):
        """不提供实际下载。"""
        return None

    def upload(self, fileitem, path, new_name=None):
        """不提供实际上传。"""
        return None

    def detail(self, fileitem):
        """原样返回文件项。"""
        return fileitem

    def copy(self, fileitem, path, new_name):
        """复制始终成功。"""
        return True

    def move(self, fileitem, path, new_name):
        """移动始终成功。"""
        return True

    def link(self, fileitem, target_file):
        """不支持硬链接。"""
        return False

    def softlink(self, fileitem, target_file):
        """不支持软链接。"""
        return False

    def usage(self):
        """不统计使用情况。"""
        return None


class _CompatStorage(_ValidPluginStorage):
    """自带 schema 属性的存储后端桩，用于验证直接交出实现类的兼容写法。"""

    schema = "compat_storage"


class _IncompleteStorage(StorageBase):
    """遗漏 usage 抽象方法的存储后端桩，用于验证契约校验拦下未落地的实现。"""

    def init_storage(self):
        """无需建立任何连接。"""

    def check(self) -> bool:
        """存储始终可用。"""
        return True

    def list(self, fileitem):
        """返回空列表。"""
        return []

    def create_folder(self, fileitem, name):
        """不提供实际创建。"""
        return None

    def get_folder(self, path):
        """不提供实际查询。"""
        return None

    def get_item(self, path):
        """不提供实际查询。"""
        return None

    def delete(self, fileitem):
        """删除始终成功。"""
        return True

    def rename(self, fileitem, name):
        """重命名始终成功。"""
        return True

    def download(self, fileitem, path=None):
        """不提供实际下载。"""
        return None

    def upload(self, fileitem, path, new_name=None):
        """不提供实际上传。"""
        return None

    def detail(self, fileitem):
        """原样返回文件项。"""
        return fileitem

    def copy(self, fileitem, path, new_name):
        """复制始终成功。"""
        return True

    def move(self, fileitem, path, new_name):
        """移动始终成功。"""
        return True

    def link(self, fileitem, target_file):
        """不支持硬链接。"""
        return False

    def softlink(self, fileitem, target_file):
        """不支持软链接。"""
        return False

    # usage 未实现，抽象方法残留


class _NotAStorage:
    """与存储基类无关的普通类。"""

    schema = "not_storage"


@pytest.fixture(autouse=True)
def _isolate_storage_registry() -> Iterator[None]:
    """快照并复原存储后端注册表，避免测试间相互污染。"""
    original_entries = dict(storage_backend_registry._entries)
    original_builtin = dict(storage_backend_registry._builtin_entries)
    try:
        yield
    finally:
        storage_backend_registry._entries.clear()
        storage_backend_registry._entries.update(original_entries)
        storage_backend_registry._builtin_entries.clear()
        storage_backend_registry._builtin_entries.update(original_builtin)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _CapableStoragePlugin:
    """声明存储后端的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "存储插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_service_instances(self):
        """返回声明的存储后端，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明存储后端时出错")
        return self._declarations


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableStoragePlugin(
        declarations=[
            _storage_declaration(
                "demo_storage",
                impl=_ValidPluginStorage,
            )
        ]
    )
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert len(declared["DemoStorage"]) == 1
    accepted = declared["DemoStorage"][0]
    assert accepted.type == "demo_storage"
    assert accepted.impl is _ValidPluginStorage


def test_projection_rejects_bare_impl_class_without_declaration():
    """裸实现类不是声明对象，宿主无从得知它属于哪一族，必须被拒。"""
    plugin = _CapableStoragePlugin(declarations=[_CompatStorage])
    projection = PluginProjection({"CompatStorage": plugin})

    declared = projection.provided_service_instances()

    assert declared["CompatStorage"] == []


@pytest.mark.parametrize(
    "declaration",
    [
        _storage_declaration("demo_storage", impl="not-a-class"),
        _storage_declaration("demo_storage", impl=None),
    ],
    ids=["impl_is_string", "impl_missing"],
)
def test_projection_rejects_impl_not_a_class(declaration):
    """impl 不是类的声明必须被拒绝。"""
    plugin = _CapableStoragePlugin(declarations=[declaration])
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert declared["DemoStorage"] == []


def test_projection_rejects_impl_not_storage_base_subclass():
    """impl 不是 StorageBase 子类的声明必须被拒绝。"""
    plugin = _CapableStoragePlugin(
        declarations=[_storage_declaration("demo_storage", impl=_NotAStorage)]
    )
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert declared["DemoStorage"] == []


def test_projection_rejects_impl_with_unimplemented_abstract_methods():
    """抽象方法未全部落地的声明必须被拒绝。"""
    plugin = _CapableStoragePlugin(
        declarations=[_storage_declaration("demo_storage", impl=_IncompleteStorage)]
    )
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert declared["DemoStorage"] == []


def test_projection_rejects_declaration_without_schema():
    """未声明非空存储标识的声明必须被拒绝。"""
    plugin = _CapableStoragePlugin(
        declarations=[_storage_declaration("", impl=_ValidPluginStorage)]
    )
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert declared["DemoStorage"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableStoragePlugin(
        declarations=[
            _storage_declaration("ok_storage", impl=_ValidPluginStorage),
            _storage_declaration("", impl=_ValidPluginStorage),
            _storage_declaration("bad_storage", impl=_NotAStorage),
        ]
    )
    projection = PluginProjection({"DemoStorage": plugin})

    declared = projection.provided_service_instances()

    assert len(declared["DemoStorage"]) == 1
    assert declared["DemoStorage"][0].type == "ok_storage"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明存储后端抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableStoragePlugin(raise_error=True)
    healthy = _CapableStoragePlugin(
        declarations=[_storage_declaration("ok_storage", impl=_ValidPluginStorage)]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_service_instances()

    assert "Broken" not in declared
    assert declared["Ok"][0].type == "ok_storage"


class _FakeStoragePlugin:
    """声明存储后端的插件桩，用于驱动插件管理器完整生命周期。"""

    plugin_name = "假想存储插件"
    plugin_version = "1.0.0"
    storage_schema = "fake_lifecycle_storage"
    storage_impl = _ValidPluginStorage

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """返回声明的固定存储后端。"""
        return [_storage_declaration(self.storage_schema, impl=self.storage_impl)]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_plugin_manager_lifecycle_registers_storage_with_instance_key_owner(
    monkeypatch, plugin_manager: PluginManager
):
    """插件启动后应以实例键为登记方登记声明的存储；停止后必须撤销，不留残留。"""
    plugin_id = _FakeStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    entry = storage_backend_registry.find("fake_lifecycle_storage")
    assert entry is not None
    assert entry.owner == plugin_id
    assert entry.distribution == ExtensionDistribution.MARKET
    assert entry.backend is _ValidPluginStorage

    plugin_manager.stop(plugin_id)

    assert storage_backend_registry.find("fake_lifecycle_storage") is None


def test_plugin_manager_config_update_resyncs_storage_registration(
    monkeypatch, plugin_manager: PluginManager
):
    """配置生效后停用实例应撤销登记，重新启用后登记应恢复。"""
    plugin_id = _FakeStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)
    assert storage_backend_registry.find("fake_lifecycle_storage") is not None

    plugin_obj = plugin_manager._running_plugins[plugin_id]
    plugin_obj.enabled = False
    plugin_manager.init_plugin(plugin_id, {})
    assert storage_backend_registry.find("fake_lifecycle_storage") is None

    plugin_obj.enabled = True
    plugin_manager.init_plugin(plugin_id, {})
    entry = storage_backend_registry.find("fake_lifecycle_storage")
    assert entry is not None
    assert entry.owner == plugin_id


def test_plugin_manager_start_skips_storage_registration_when_plugin_raises(
    monkeypatch, plugin_manager: PluginManager
):
    """插件的 provides_service_instances 抛异常时不应阻断插件加载。"""

    class _BrokenStoragePlugin(_FakeStoragePlugin):
        """声明存储后端时抛异常的插件桩。"""

        def provides_service_instances(self):
            """模拟插件实现出错。"""
            raise RuntimeError("声明存储后端时出错")

    plugin_id = _BrokenStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_BrokenStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    assert plugin_id in plugin_manager._running_plugins
    assert storage_backend_registry.find("fake_lifecycle_storage") is None


def test_plugin_stop_restores_builtin_value_when_override_is_removed(
    monkeypatch, plugin_manager: PluginManager
):
    """插件覆盖内建存储标识后停用，内建取值必须按最近一次内建登记还原。"""
    storage_backend_registry.register(
        _ValidPluginStorage,
        distribution=ExtensionDistribution.BUILTIN,
        owner="BuiltinModule",
        storage_id="shared_storage",
    )

    class _OverridingStoragePlugin(_FakeStoragePlugin):
        """覆盖内建存储标识的插件桩。"""

        storage_schema = "shared_storage"
        storage_impl = _CompatStorage

    plugin_id = _OverridingStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_OverridingStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    overridden = storage_backend_registry.find("shared_storage")
    assert overridden is not None
    assert overridden.owner == plugin_id
    assert overridden.distribution == ExtensionDistribution.MARKET
    assert overridden.backend is _CompatStorage

    plugin_manager.stop(plugin_id)

    restored = storage_backend_registry.find("shared_storage")
    assert restored is not None
    assert restored.owner == "BuiltinModule"
    assert restored.distribution == ExtensionDistribution.BUILTIN
    assert restored.backend is _ValidPluginStorage


def test_plugin_stop_only_revokes_its_own_registration(
    monkeypatch, plugin_manager: PluginManager
):
    """停用的插件曾登记过的标识若已被另一个登记方接管，停用不得波及新登记方。"""
    plugin_id = _FakeStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)
    assert storage_backend_registry.find("fake_lifecycle_storage").owner == plugin_id

    # 另一个登记方在插件仍运行期间接管了同一存储标识
    storage_backend_registry.register(
        _CompatStorage,
        distribution=ExtensionDistribution.MARKET,
        owner="OtherOwner@default",
        storage_id="fake_lifecycle_storage",
    )

    plugin_manager.stop(plugin_id)

    entry = storage_backend_registry.find("fake_lifecycle_storage")
    assert entry is not None
    assert entry.owner == "OtherOwner@default"
    assert entry.backend is _CompatStorage


class _StorageFanOutPlugin:
    """按实例配置声明存储或按需抛异常的插件桩，用于驱动多实例扇出。"""

    plugin_name = "存储扇出插件"
    plugin_version = "1.0.0"

    def __init__(self):
        self.config: dict = {}

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息。"""
        self.config = config or {}

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self.config.get("enable", True))

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_service_instances(self):
        """按配置声明存储后端，或按配置模拟实现出错。"""
        if self.config.get("raise_error"):
            raise RuntimeError("声明存储后端时出错")
        schema = self.config.get("schema")
        if not schema:
            return []
        return [_storage_declaration(schema, impl=_ValidPluginStorage)]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_sibling_instance_exception_does_not_block_healthy_instance(
    monkeypatch, plugin_manager: PluginManager
):
    """一个实例的存储声明抛异常时，兄弟实例的登记与运行态都不受影响。"""
    plugin_id = _StorageFanOutPlugin.__name__
    second_key = f"{plugin_id}@second"
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_StorageFanOutPlugin],
    )
    monkeypatch.setattr(
        plugin_manager, "_plugin_instance_ids", lambda pid: ["default", "second"]
    )
    monkeypatch.setattr(
        plugin_manager,
        "get_plugin_config",
        lambda pid: {
            plugin_id: {"enable": True, "schema": "fanout_default_storage"},
            second_key: {"enable": True, "raise_error": True},
        }.get(pid, {}),
    )

    plugin_manager.start(pid=plugin_id)

    assert plugin_id in plugin_manager._running_plugins
    assert second_key in plugin_manager._running_plugins
    entry = storage_backend_registry.find("fanout_default_storage")
    assert entry is not None
    assert entry.owner == plugin_id


def test_storage_base_qualified_name_still_resolves_to_the_real_class() -> None:
    """契约校验用的存储基类限定名必须仍指向真实的 StorageBase。

    校验走 MRO 上的模块与限定名比对，以避开 app.runtime 反向依赖 app.modules 的
    禁令。代价是基类一旦改名或搬家，比对会静默失配、把全部插件存储声明拒之门外，
    而错误信息仍写着「不是 StorageBase 的子类」，极具误导性。本用例把那种静默
    退化变成一次响亮的失败。
    """
    assert (
        f"{StorageBase.__module__}.{StorageBase.__qualname__}"
        == storage._STORAGE_BASE_QUALIFIED_NAME
    )


@pytest.fixture(autouse=True)
def _clean_extension_scoped_warnings() -> Iterator[None]:
    """每个用例前后都清空扩展级去重告警记录，避免用例间互相掩盖。"""
    extension_scoped._extension_scoped_warnings_seen.clear()
    yield
    extension_scoped._extension_scoped_warnings_seen.clear()


def _start_fanout_instances(
    monkeypatch, plugin_manager: PluginManager, schemas: dict
) -> str:
    """按实例键到存储标识的映射启动存储扇出插件的多个实例。

    :param monkeypatch: pytest monkeypatch
    :param plugin_manager: 插件管理器
    :param schemas: 实例键到该实例声明的存储标识的映射
    :return: 插件ID
    """
    plugin_id = _StorageFanOutPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_StorageFanOutPlugin],
    )
    monkeypatch.setattr(
        plugin_manager,
        "_plugin_instance_ids",
        lambda pid: ["default", "home"],
    )
    monkeypatch.setattr(
        plugin_manager,
        "get_plugin_config",
        lambda pid: {"enable": True, "schema": schemas.get(pid)},
    )
    plugin_manager.start(pid=plugin_id)
    return plugin_id


def test_sibling_instances_register_one_storage_identity_once(
    monkeypatch, plugin_manager: PluginManager
):
    """两个实例声明同一存储标识时只登记一条，归属默认实例。"""
    plugin_id = _StorageFanOutPlugin.__name__
    _start_fanout_instances(
        monkeypatch,
        plugin_manager,
        {plugin_id: "shared_fanout_storage", f"{plugin_id}@home": "shared_fanout_storage"},
    )

    entry = storage_backend_registry.find("shared_fanout_storage")
    assert entry is not None
    assert entry.owner == plugin_id
    assert [item for item in storage_backend_registry.entries()
            if item.storage_id == "shared_fanout_storage"] == [entry]


def test_stopping_the_storage_owner_hands_the_identity_to_its_sibling(
    monkeypatch, plugin_manager: PluginManager
):
    """登记方停止后存储标识由仍在运行的兄弟实例接手。"""
    plugin_id = _StorageFanOutPlugin.__name__
    _start_fanout_instances(
        monkeypatch,
        plugin_manager,
        {plugin_id: "shared_fanout_storage", f"{plugin_id}@home": "shared_fanout_storage"},
    )

    plugin_manager.stop(plugin_id, instance_id="default")

    entry = storage_backend_registry.find("shared_fanout_storage")
    assert entry is not None
    assert entry.owner == f"{plugin_id}@home"


def test_storage_identity_is_recycled_after_every_sibling_stops(
    monkeypatch, plugin_manager: PluginManager
):
    """全部实例停止后该存储标识必须被回收干净。"""
    plugin_id = _StorageFanOutPlugin.__name__
    _start_fanout_instances(
        monkeypatch,
        plugin_manager,
        {plugin_id: "shared_fanout_storage", f"{plugin_id}@home": "shared_fanout_storage"},
    )

    plugin_manager.stop(plugin_id, instance_id="default")
    plugin_manager.stop(plugin_id, instance_id="home")

    assert storage_backend_registry.find("shared_fanout_storage") is None


def test_siblings_declaring_distinct_storage_identities_keep_both(
    monkeypatch, plugin_manager: PluginManager
):
    """不同实例声明不同存储标识时两条登记并存，各自归属声明它的实例。"""
    plugin_id = _StorageFanOutPlugin.__name__
    _start_fanout_instances(
        monkeypatch,
        plugin_manager,
        {plugin_id: "default_fanout_storage", f"{plugin_id}@home": "home_fanout_storage"},
    )

    assert storage_backend_registry.find("default_fanout_storage").owner == plugin_id
    assert (
        storage_backend_registry.find("home_fanout_storage").owner
        == f"{plugin_id}@home"
    )


class _StorageAndServicePlugin:
    """一条钩子里同时声明存储类型与下载器类型的插件桩。"""

    plugin_name = "双族插件"
    plugin_version = "1.0.0"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """声明一个存储类型与一个下载器类型，两族共用同一条钩子。"""
        return [
            _storage_declaration(
                "catalog_storage",
                impl=_ValidPluginStorage,
                name="目录存储",
                multi_instance=False,
                config_schema={
                    "type": "object", "properties": {"token": {"type": "string"}}
                },
            ),
            ServiceInstanceDeclaration(
                capability="downloader",
                type="catalog_downloader",
                name="目录下载器",
                impl=_CatalogDownloader,
            ),
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


class _CatalogDownloader:
    """契约合规的服务实例实现桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs):
        """记录宿主传入的实例名。"""
        self.name = name

    def is_inactive(self) -> bool:
        """回答连接是否已断开，宿主的十分钟重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，宿主判定失活后直调它。"""
        return True


def _start_catalog_plugin(monkeypatch, plugin_manager: PluginManager) -> str:
    """启动同时声明两族类型的插件桩。

    :param monkeypatch: pytest 的猴子补丁夹具
    :param plugin_manager: 插件管理器
    :return: 插件实例键
    """
    plugin_id = _StorageAndServicePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_StorageAndServicePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})
    plugin_manager.start(pid=plugin_id)
    return plugin_id


def test_plugin_storage_declaration_also_enters_the_service_type_catalog(
    monkeypatch, plugin_manager: PluginManager
):
    """存储声明同时进类型目录：提供方、份数、契约与界面都按声明落账。"""
    plugin_id = _start_catalog_plugin(monkeypatch, plugin_manager)
    try:
        entry = service_instance_registry.find("storage", "catalog_storage")

        assert entry is not None
        assert entry.owner == plugin_id
        assert entry.name == "目录存储"
        assert entry.multi_instance is False
        assert entry.config_schema == {
            "type": "object", "properties": {"token": {"type": "string"}}
        }
        assert entry.factory is not None
    finally:
        plugin_manager.stop(plugin_id)

    assert service_instance_registry.find("storage", "catalog_storage") is None


def test_two_families_declared_in_one_hook_both_register(
    monkeypatch, plugin_manager: PluginManager
):
    """一条钩子里的两族声明各自落账：类型目录两条，存储后端注册表一条。"""
    plugin_id = _start_catalog_plugin(monkeypatch, plugin_manager)
    try:
        assert service_instance_registry.find("storage", "catalog_storage") is not None
        assert service_instance_registry.find(
            "downloader", "catalog_downloader"
        ) is not None
        assert storage_backend_registry.find("catalog_storage") is not None
    finally:
        plugin_manager.stop(plugin_id)


def test_catalog_factory_builds_the_backend_for_the_declared_instance(
    monkeypatch, plugin_manager: PluginManager
):
    """类型目录里的工厂按单条配置构造该实例的存储对象，归属随配置而来。"""
    plugin_id = _start_catalog_plugin(monkeypatch, plugin_manager)
    try:
        entry = service_instance_registry.find("storage", "catalog_storage")

        storage = entry.factory(StorageConf(type="catalog_storage", name="工作号"))

        assert isinstance(storage, _ValidPluginStorage)
        assert storage.storage_instance == "工作号"
        assert storage.storage_is_bare_token is False
    finally:
        plugin_manager.stop(plugin_id)


class _FanOutStoragePlugin:
    """声明一个多实例存储类型的插件桩，按类属性决定写不写工厂。"""

    plugin_name = "存储扇出类型插件"
    plugin_version = "1.0.0"
    declares_factory = False

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_service_instances(self):
        """声明存储类型，按类属性决定是否附带自己的实例工厂。"""
        return [_storage_declaration(
            "fanout_type_storage",
            impl=_ValidPluginStorage,
            name="扇出存储",
            factory=self._declared_factory if self.declares_factory else None,
        )]

    @staticmethod
    def _declared_factory(conf):
        """声明自带的工厂，返回可与宿主默认工厂区分的标记对象。

        :param conf: 单条存储实例配置
        :return: 标记该实例由声明自带工厂构造的二元组
        """
        return ("declared", conf.name)

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


class _FactoryStoragePlugin(_FanOutStoragePlugin):
    """声明存储类型并自带实例工厂的插件桩。"""

    declares_factory = True


def _fan_out_instances(monkeypatch, plugin_manager: PluginManager, plugin_class) -> dict:
    """启动插件桩并按当前存储配置取出该类型扇出的全部实例。

    :param monkeypatch: pytest 的猴子补丁夹具
    :param plugin_manager: 插件管理器
    :param plugin_class: 插件桩类
    :return: 实例名到实例的映射
    """
    helper = StorageHelper()
    helper.save_storagies([
        StorageConf(type="fanout_type_storage", name="工作号", config={"k": "a"}),
        StorageConf(type="fanout_type_storage", name="备用号", config={"k": "b"}),
    ])
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [plugin_class],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})
    plugin_id = plugin_class.__name__
    plugin_manager.start(pid=plugin_id)
    try:
        adapter = next(
            item for item in service_instance_registry.adapters("storage")
            if item.entry.service_type == "fanout_type_storage"
        )
        return adapter.get_instances()
    finally:
        plugin_manager.stop(plugin_id)
        helper.save_storagies([])


def test_storage_type_without_a_factory_fans_out_through_the_host_default(
    monkeypatch, plugin_manager: PluginManager
):
    """不写工厂的存储类型照常扇出：宿主默认工厂按实例归属构造后端，作者零样板。"""
    instances = _fan_out_instances(monkeypatch, plugin_manager, _FanOutStoragePlugin)

    assert sorted(instances) == ["备用号", "工作号"]
    assert all(isinstance(item, _ValidPluginStorage) for item in instances.values())
    assert {
        name: item.storage_instance for name, item in instances.items()
    } == {"工作号": "工作号", "备用号": "备用号"}


def test_declared_factory_is_used_instead_of_the_host_default(
    monkeypatch, plugin_manager: PluginManager
):
    """声明自带工厂时走它，宿主默认工厂让位。"""
    instances = _fan_out_instances(monkeypatch, plugin_manager, _FactoryStoragePlugin)

    assert instances == {
        "工作号": ("declared", "工作号"), "备用号": ("declared", "备用号")
    }
