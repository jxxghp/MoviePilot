"""插件多实例运行时内核的行为契约测试。

覆盖同一插件类扇出多个实例后的四条底线：实例之间的配置、数据、数据目录与启用态
互不干扰；单个实例的停用与停止不波及兄弟实例；没有任何实例配置记录的存量插件回落
到单个默认实例、行为与不区分实例时完全一致；同一插件的多个实例在分发面各自可见，
不会互相覆盖。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.db.models.pluginconfig import PluginConfig
from app.db.models.plugindata import PluginData
from app.foundation.singleton import Singleton
from app.sdk.extension import _PluginBase
from app.runtime.event.binding import EventBindingResolver, EventHandlerBinding
from app.runtime.events import EventManager
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.registry.plugin import PluginRegistry
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.runtime.extensions.plugin_manager import PluginManager
from app.startup.plugins_initializer import (
    _list_plugin_instance_ids,
    _read_plugin_instance_config,
    _write_plugin_instance_config,
)

# 生命周期用例的插件标识必须与插件主类名一致，管理器按类名筛选加载目标
PLUGIN_ID = "_LifecyclePlugin"
DEMO_ID = "_DemoPlugin"
SECOND_INSTANCE = "second"
SECOND_KEY = f"{PLUGIN_ID}@{SECOND_INSTANCE}"
DEMO_SECOND_KEY = f"{DEMO_ID}@{SECOND_INSTANCE}"


class _DemoPlugin(_PluginBase):
    """只实现抽象契约的最小插件，用于驱动实例级持久化访问。"""

    plugin_name = "多实例演示"
    plugin_version = "1.0.0"

    def init_plugin(self, config: dict = None):
        """生效配置信息。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool((self.get_config() or {}).get("enable"))

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


class _LifecyclePlugin:
    """驱动管理器完整生命周期、无参构造的最小插件桩。"""

    plugin_name = "多实例生命周期插件"
    plugin_version = "1.0.0"

    def __init__(self):
        """初始化未生效任何配置的实例。"""
        self.config: dict = {}
        self.stopped = False
        self.events: list = []

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息。"""
        self.config = config or {}

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self.config.get("enable"))

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def get_module(self) -> dict:
        """声明本实例参与分发的方法表。"""
        return {"recognize_media": self.recognize_media}

    def recognize_media(self, *_args, **_kwargs):
        """按本实例配置认领分发请求，未配置答案时让出。"""
        return self.config.get("answer")

    def handle(self, _event) -> None:
        """记录一次事件处理。"""
        self.events.append(_event)

    def close(self) -> None:
        """释放测试桩持有的资源。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务。"""
        self.stopped = True


class _IdentityAwarePlugin(_LifecyclePlugin):
    """在构造签名上显式接受插件标识与实例标识的插件桩。"""

    def __init__(self, plugin_id: str, instance_id: str):
        """记录宿主注入的标识。

        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        """
        super().__init__()
        self.injected = (plugin_id, instance_id)


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
    yield
    plugin_manager_module._plugin_database_ensure = saved_ensure
    plugin_manager_module._plugin_database_release = saved_release
    plugin_manager_module._plugin_database_destroy = saved_destroy


@pytest.fixture(autouse=True)
def _restore_event_handler_switches() -> Iterator[None]:
    """快照并复原事件总线的停用集合，避免用例间相互污染。

    事件总线是进程级单例，插件启停会在其中登记类级与实例级停用状态。
    """
    manager = EventManager()
    disabled_classes = manager._EventManager__disabled_classes
    disabled_instances = manager._EventManager__disabled_instances
    saved_classes = set(disabled_classes)
    saved_instances = set(disabled_instances)
    yield
    disabled_classes.clear()
    disabled_classes.update(saved_classes)
    disabled_instances.clear()
    disabled_instances.update(saved_instances)


@pytest.fixture
def production_plugin_config_storage() -> Iterator[None]:
    """按启动组合根同款接线，把插件配置端口接到真实 PluginConfigOper。"""
    original = get_plugin_storage()
    configure_plugin_storage(PluginStorage(
        read_config=_read_plugin_instance_config,
        write_config=_write_plugin_instance_config,
        list_instances=_list_plugin_instance_ids,
    ))
    yield
    configure_plugin_storage(original)


@pytest.fixture
def plugin_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件数据根目录指向临时目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugin_paths_module, "settings", SimpleNamespace(PLUGIN_DATA_PATH=root))
    return root


def _install_lifecycle_plugin(
    monkeypatch: pytest.MonkeyPatch,
    manager: PluginManager,
    plugin_class: type,
    instance_ids: List[str],
    configs: Dict[str, dict],
) -> None:
    """把插件桩接入管理器的加载路径，并给定实例清单与各实例配置。

    :param monkeypatch: 用例级补丁器
    :param manager: 目标插件管理器
    :param plugin_class: 插件类
    :param instance_ids: 该插件的实例清单
    :param configs: 实例键到该实例配置的映射
    """
    monkeypatch.setattr(
        manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [plugin_class],
    )
    monkeypatch.setattr(manager, "_plugin_instance_ids", lambda pid: list(instance_ids))
    monkeypatch.setattr(manager, "get_plugin_config", lambda pid: dict(configs.get(pid, {})))
    plugin_manager_module._plugin_database_ensure = lambda _pid, _iid: None
    plugin_manager_module._plugin_database_release = lambda _pid: None
    plugin_manager_module._plugin_database_destroy = lambda _pid, _iid: None


class _PluginCatalog:
    """提供固定插件方法表的内存目录。"""

    def __init__(self, modules: dict) -> None:
        """保存测试提供的插件模块快照。"""
        self.modules = modules

    def get_plugin_modules(self) -> dict:
        """返回当前插件模块快照。"""
        return self.modules


class _ModuleCatalog:
    """不提供任何宿主模块的内存目录。"""

    @staticmethod
    def get_running_modules(_method: str) -> list:
        """返回空的宿主模块列表。"""
        return []

    @staticmethod
    def providers_for(_method: str) -> tuple:
        """返回空的宿主提供者序列。"""
        return ()


def _dispatcher(plugin_modules: dict) -> ModuleInvocationDispatcher:
    """构造只挂插件目录的内存调度器。

    :param plugin_modules: `(实例键, 展示名)` 到方法表的映射
    :return: 模块调度器
    """
    return ModuleInvocationDispatcher(
        module_catalog=_ModuleCatalog(),
        plugin_catalog=_PluginCatalog(plugin_modules),
        plugin_error_handler=lambda *args, **kwargs: None,
        system_error_handler=lambda *args, **kwargs: None,
        rate_limit_handler=lambda *args, **kwargs: None,
    )


def test_sibling_instances_hold_independent_config_data_and_paths(
    db, plugin_data_root, production_plugin_config_storage
):
    """同一插件的两个实例各自持有配置、数据、数据目录与启用态，互不干扰。"""
    db.watermark(PluginConfig)
    db.watermark(PluginData)
    default_instance = _DemoPlugin(plugin_id=DEMO_ID)
    second_instance = _DemoPlugin(plugin_id=DEMO_ID, instance_id=SECOND_INSTANCE)

    default_instance.update_config({"enable": True, "token": "default-token"})
    second_instance.update_config({"enable": False, "token": "second-token"})
    default_instance.save_data("cursor", 1)
    second_instance.save_data("cursor", 2)

    assert default_instance.get_config() == {"enable": True, "token": "default-token"}
    assert second_instance.get_config() == {"enable": False, "token": "second-token"}
    assert default_instance.get_data("cursor") == 1
    assert second_instance.get_data("cursor") == 2
    assert default_instance.get_state() is True
    assert second_instance.get_state() is False
    default_path = default_instance.get_data_path()
    second_path = second_instance.get_data_path()
    assert default_path != second_path
    assert second_path.is_relative_to(plugin_data_root / DEMO_ID / SECOND_INSTANCE)

    default_instance.del_data("cursor")

    assert default_instance.get_data("cursor") is None
    assert second_instance.get_data("cursor") == 2


def test_default_instance_addressing_is_identical_to_plugin_id(
    db, plugin_data_root, production_plugin_config_storage
):
    """不指定实例的存量插件落在默认实例上，实例键退化为裸插件标识。"""
    db.watermark(PluginConfig)
    legacy = _DemoPlugin(plugin_id=DEMO_ID)
    explicit_default = _DemoPlugin(plugin_id=DEMO_ID, instance_id=DEFAULT_INSTANCE_ID)

    legacy.update_config({"enable": True})

    assert legacy.instance_id == DEFAULT_INSTANCE_ID
    assert legacy.get_instance_key() == DEMO_ID
    assert explicit_default.get_config() == {"enable": True}
    assert explicit_default.get_data_path() == legacy.get_data_path()


def test_plugin_instance_ids_fall_back_to_single_default_without_records(
    db, production_plugin_config_storage
):
    """一条实例配置记录都没有时回落到单个默认实例。"""
    db.watermark(PluginConfig)

    assert PluginManager._plugin_instance_ids("_NeverConfiguredPlugin") == [
        DEFAULT_INSTANCE_ID
    ]


def test_plugin_instance_ids_list_default_first(db, production_plugin_config_storage):
    """已登记多个实例时默认实例排在最前，其余按标识升序。"""
    db.watermark(PluginConfig)
    _write_plugin_instance_config(DEMO_ID, {"enable": True})
    from app.db.oper.pluginconfig import PluginConfigOper

    PluginConfigOper().upsert(DEMO_ID, "beta", {"config_data": {}})
    PluginConfigOper().upsert(DEMO_ID, "alpha", {"config_data": {}})

    assert PluginManager._plugin_instance_ids(DEMO_ID) == [
        DEFAULT_INSTANCE_ID,
        "alpha",
        "beta",
    ]


def test_start_fans_out_registry_by_instance_key(monkeypatch, plugin_manager):
    """按实例清单扇出后，运行态表以实例键索引，类表仍按插件标识索引一份。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {PLUGIN_ID: {"enable": True}, SECOND_KEY: {"enable": False}},
    )

    plugin_manager.start(pid=PLUGIN_ID)

    assert plugin_manager.get_running_instance_keys(PLUGIN_ID) == [
        PLUGIN_ID,
        SECOND_KEY,
    ]
    assert plugin_manager.get_running_plugin_ids() == [PLUGIN_ID]
    assert plugin_manager.get_plugin_ids() == [PLUGIN_ID]
    assert plugin_manager._running_plugins[PLUGIN_ID] is not (
        plugin_manager._running_plugins[SECOND_KEY]
    )
    assert plugin_manager._running_plugins[PLUGIN_ID].config == {"enable": True}
    assert plugin_manager._running_plugins[SECOND_KEY].config == {"enable": False}
    # 任一实例启用即视为插件启用，单个实例的启用态按实例键查询
    assert plugin_manager.get_plugin_state(PLUGIN_ID) is True
    assert plugin_manager.get_plugin_state(SECOND_KEY) is False


def test_start_writes_identity_onto_parameterless_plugin(monkeypatch, plugin_manager):
    """无参 __init__ 的插件能被正常实例化，并无条件拿到标识属性。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {},
    )

    plugin_manager.start(pid=PLUGIN_ID)

    default_instance = plugin_manager._running_plugins[PLUGIN_ID]
    second_instance = plugin_manager._running_plugins[SECOND_KEY]
    assert (default_instance.plugin_id, default_instance.instance_id) == (
        PLUGIN_ID,
        DEFAULT_INSTANCE_ID,
    )
    assert (second_instance.plugin_id, second_instance.instance_id) == (
        PLUGIN_ID,
        SECOND_INSTANCE,
    )


def test_start_injects_identity_when_constructor_accepts_it(monkeypatch, plugin_manager):
    """插件 __init__ 声明了标识参数时由宿主注入，属性回写结果保持一致。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _IdentityAwarePlugin,
        [SECOND_INSTANCE],
        {},
    )

    plugin_manager.start(pid="_IdentityAwarePlugin")

    instance = plugin_manager._running_plugins["_IdentityAwarePlugin@second"]
    assert instance.injected == ("_IdentityAwarePlugin", SECOND_INSTANCE)
    assert (instance.plugin_id, instance.instance_id) == (
        "_IdentityAwarePlugin",
        SECOND_INSTANCE,
    )


def test_stop_single_instance_keeps_sibling_running(monkeypatch, plugin_manager):
    """停止一个实例后兄弟实例继续在运行，插件类不被摘除。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {PLUGIN_ID: {"enable": True}, SECOND_KEY: {"enable": True}},
    )
    plugin_manager.start(pid=PLUGIN_ID)
    sibling = plugin_manager._running_plugins[SECOND_KEY]

    plugin_manager.stop(PLUGIN_ID, DEFAULT_INSTANCE_ID)

    assert plugin_manager.get_running_instance_keys(PLUGIN_ID) == [SECOND_KEY]
    assert plugin_manager.get_plugin_ids() == [PLUGIN_ID]
    assert sibling.stopped is False


def test_stop_single_instance_skips_module_cache_and_database_recycle(
    monkeypatch, plugin_manager
):
    """兄弟实例仍在运行时不清理模块缓存、不释放该插件的数据库连接。"""
    cleared: list = []
    released: list = []
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {},
    )
    plugin_manager.start(pid=PLUGIN_ID)
    monkeypatch.setattr(
        plugin_manager,
        "_clear_plugin_modules",
        lambda pid=None: cleared.append(pid),
    )
    plugin_manager_module._plugin_database_release = lambda pid: released.append(pid)

    plugin_manager.stop(PLUGIN_ID, DEFAULT_INSTANCE_ID)

    assert cleared == []
    assert released == []

    plugin_manager.stop(PLUGIN_ID, SECOND_INSTANCE)

    assert cleared == [PLUGIN_ID]
    assert released == [PLUGIN_ID]


def test_stop_without_instance_recycles_the_whole_family(monkeypatch, plugin_manager):
    """不指定实例时按族回收该插件的全部实例。"""
    cleared: list = []
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {},
    )
    plugin_manager.start(pid=PLUGIN_ID)
    monkeypatch.setattr(
        plugin_manager,
        "_clear_plugin_modules",
        lambda pid=None: cleared.append(pid),
    )

    plugin_manager.stop(PLUGIN_ID)

    assert plugin_manager.get_running_instance_keys(PLUGIN_ID) == []
    assert plugin_manager.get_plugin_ids() == []
    assert cleared == [PLUGIN_ID]


def test_event_resolver_returns_every_instance_binding(monkeypatch, plugin_manager):
    """事件解析器返回该类全部实例的绑定，每条带正确的实例键。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LifecyclePlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {},
    )
    monkeypatch.setattr(plugin_manager, "_plugins", {PLUGIN_ID: _LifecyclePlugin})
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {
            PLUGIN_ID: _LifecyclePlugin(),
            SECOND_KEY: _LifecyclePlugin(),
        },
    )

    bindings = plugin_manager.resolve_event_handler_instance(_LifecyclePlugin)

    assert [binding.instance_key for binding in bindings] == [PLUGIN_ID, SECOND_KEY]
    assert all(binding.owner_name == _LifecyclePlugin.plugin_name for binding in bindings)
    assert bindings[0].instance is not bindings[1].instance


def test_disabling_one_instance_leaves_sibling_binding_resolvable():
    """单个实例的事件停用不影响兄弟实例的绑定解析。"""
    manager = EventManager()
    default_binding_instance = _LifecyclePlugin()
    sibling_instance = _LifecyclePlugin()
    resolver_bindings = [
        EventHandlerBinding(
            instance=default_binding_instance,
            owner_name=PLUGIN_ID,
            instance_key=PLUGIN_ID,
        ),
        EventHandlerBinding(
            instance=sibling_instance,
            owner_name=SECOND_KEY,
            instance_key=SECOND_KEY,
        ),
    ]
    # 事件总线是进程级单例，先清空已登记解析器，避免其它用例遗留的解析器抢先认领
    resolvers = manager._EventManager__handler_instance_resolvers
    saved_resolvers = dict(resolvers)
    resolvers.clear()
    manager.register_handler_instance_resolver(
        "multi_instance_test",
        lambda owner: resolver_bindings if owner is _LifecyclePlugin else None,
    )
    try:
        resolver: EventBindingResolver = manager._EventManager__binding_resolver

        assert [item[1].instance_key for item in resolver.resolve(_LifecyclePlugin.handle)] == [
            PLUGIN_ID,
            SECOND_KEY,
        ]

        manager.disable_event_handler(_LifecyclePlugin, PLUGIN_ID)

        assert [item[1].instance_key for item in resolver.resolve(_LifecyclePlugin.handle)] == [
            SECOND_KEY
        ]
        # 实例级停用不得连带停掉整类，否则兄弟实例也会被调度前的类级门槛拦掉
        registry = manager._EventManager__registry
        assert registry.is_handler_enabled(_LifecyclePlugin.handle) is True

        manager.enable_event_handler(_LifecyclePlugin, PLUGIN_ID)

        assert [item[1].instance_key for item in resolver.resolve(_LifecyclePlugin.handle)] == [
            PLUGIN_ID,
            SECOND_KEY,
        ]
    finally:
        manager.enable_event_handler(_LifecyclePlugin, PLUGIN_ID)
        manager.enable_event_handler(_LifecyclePlugin, SECOND_KEY)
        manager.enable_event_handler(_LifecyclePlugin)
        resolvers.clear()
        resolvers.update(saved_resolvers)


def test_registry_indexes_running_instances_by_instance_key():
    """注册表按实例键索引运行实例，按族回落取类并按族去重列插件标识。"""
    registry = PluginRegistry()
    plugin_class = type("Demo", (), {})
    registry.classes["Demo"] = plugin_class
    registry.running["Demo"] = object()
    registry.running["Demo@a"] = object()

    assert registry.instance_keys("Demo") == ["Demo", "Demo@a"]
    assert registry.plugin_class("Demo@a") is plugin_class
    assert registry.has_class("Demo@a") is True
    assert registry.running_plugin_ids() == ["Demo"]
    # 多个实例在运行时按插件标识不回落，避免取到调用方并未指定的那一个
    assert registry.instance("Demo@a") is registry.running["Demo@a"]

    registry.remove_instance("Demo")

    assert registry.instance_keys("Demo") == ["Demo@a"]
    assert registry.has_class("Demo") is True

    registry.remove_instance("Demo@a")

    assert registry.has_class("Demo") is False


def test_projection_keeps_every_instance_module_table():
    """两个实例挂同一契约名时按实例键各占一格，不互相覆盖。"""
    first = _LifecyclePlugin()
    first.init_plugin({"enable": True, "answer": "first"})
    second = _LifecyclePlugin()
    second.init_plugin({"enable": True, "answer": "second"})
    projection = PluginProjection({PLUGIN_ID: first, SECOND_KEY: second})

    modules = projection.modules()

    assert list(modules) == [
        (PLUGIN_ID, _LifecyclePlugin.plugin_name),
        (SECOND_KEY, _LifecyclePlugin.plugin_name),
    ]
    assert modules[(PLUGIN_ID, _LifecyclePlugin.plugin_name)]["recognize_media"]() == "first"
    assert modules[(SECOND_KEY, _LifecyclePlugin.plugin_name)]["recognize_media"]() == "second"


def test_projection_filters_by_plugin_id_and_instance_key():
    """插件标识命中该插件全部实例，实例键只命中该实例。"""
    first = _LifecyclePlugin()
    first.init_plugin({"enable": True, "answer": "first"})
    second = _LifecyclePlugin()
    second.init_plugin({"enable": True, "answer": "second"})
    projection = PluginProjection({PLUGIN_ID: first, SECOND_KEY: second})

    assert len(projection.modules(PLUGIN_ID)) == 2
    assert list(projection.modules(SECOND_KEY)) == [
        (SECOND_KEY, _LifecyclePlugin.plugin_name)
    ]


def test_dispatch_sees_both_instances_and_unicast_takes_first_claimer():
    """广播与多播逐个触达全部实例，单播按实例登记顺序取首个非空答案。"""
    calls: list = []

    def _answer(name: str, value):
        """生成记录调用并返回预置结果的实例方法替身。"""

        def call(*_args, **_kwargs):
            """记录本次调用并返回预置结果。"""
            calls.append(name)
            return value

        return call

    plugin_modules = {
        (PLUGIN_ID, "多实例生命周期插件"): {
            "recognize_media": _answer("default", None),
        },
        (SECOND_KEY, "多实例生命周期插件"): {
            "recognize_media": _answer("second", "命中"),
        },
    }

    dispatcher = _dispatcher(plugin_modules)
    dispatcher.broadcast("recognize_media")
    assert calls == ["default", "second"]

    calls.clear()
    assert dispatcher.multicast("recognize_media") == ["命中"]
    assert calls == ["default", "second"]

    calls.clear()
    assert dispatcher.unicast("recognize_media") == "命中"
    assert calls == ["default", "second"]

    # 首个实例认领后单播即短路，后续兄弟实例不会被调用
    calls.clear()
    plugin_modules[(PLUGIN_ID, "多实例生命周期插件")]["recognize_media"] = _answer(
        "default", "先手"
    )
    assert dispatcher.unicast("recognize_media") == "先手"
    assert calls == ["default"]


def test_get_plugin_remote_entry_downgrades_instance_key_to_plugin_id():
    """联邦入口地址按插件标识拼目录，实例键降级后指向同一份共享代码产物。

    联邦构建产物属于插件本身而非某个实例，实例键的 @ 分隔符不能出现在目录
    路径里，否则指向一个不存在的目录。
    """
    default_url = PluginManager.get_plugin_remote_entry(PLUGIN_ID, "dist/assets")
    instance_url = PluginManager.get_plugin_remote_entry(SECOND_KEY, "dist/assets")

    assert instance_url == default_url
    assert "@" not in instance_url
    assert instance_url == f"/plugin/file/{PLUGIN_ID.lower()}/dist/assets/remoteEntry.js"
