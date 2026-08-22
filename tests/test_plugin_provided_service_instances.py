"""插件声明服务实例类型链路测试：契约校验、登记归属、实例扇出与停用回收。

服务实例族（下载器、媒体服务器、消息通知）与其它扩展点的区别是「有没有」不是
终点：用户按类型配置 N 个具名实例，宿主对每条配置构造一个实例。本文件覆盖从
声明契约到 `ServiceBaseHelper.get_services()` 的整条取用链。
"""

from typing import Any, Dict, Iterator, Iterable, List, Optional

import pytest

from app.application.downloader import DownloaderHelper
from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.admission import extension_scoped
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.service_config import (
    configure_service_instance_config_reader,
    service_capability,
)
from app.runtime.extensions.registry.service_instance import (
    declared_service_instances,
    service_instance_registry,
)
from app.schemas.types import ModuleType


class _DemoDownloader:
    """契约合规的下载器客户端桩，按 impl(name=..., **config) 构造。"""

    def __init__(self, name: Optional[str] = None, host: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名与配置内容。"""
        self.name = name
        self.host = host
        self.extra = kwargs

    def is_inactive(self) -> bool:
        """回答连接是否已断开，宿主的十分钟重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，宿主判定失活后直调它。"""
        return True


class _ExplodingDownloader(_DemoDownloader):
    """构造时按配置内容决定是否抛异常的下载器客户端桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """配置里带 broken 标记时模拟连接初始化失败。"""
        if kwargs.get("broken"):
            raise RuntimeError(f"下载器 {name} 初始化失败")
        super().__init__(name=name, **kwargs)


class _DemoNotifier:
    """契约合规的消息通道客户端桩，只带消息通知族取用链上必须在场的方法。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def get_state(self) -> bool:
        """回答通道是否就绪，宿主的连通性测试直调它。"""
        return True


class _NoNameDownloader:
    """构造签名不接受关键字 name 的客户端桩。"""

    def __init__(self, host: str):
        """只接受 host，宿主固定填入的 name 无处可去。"""
        self.host = host


class _PositionalOnlyDownloader:
    """构造签名含仅限位置必填参数的客户端桩。"""

    def __init__(self, token, /, name: Optional[str] = None, **kwargs: Any):
        """token 只能按位置传入，用户配置再全也补不上。"""
        self.token = token
        self.name = name


def _demo_downloader_factory(conf: Any) -> _DemoDownloader:
    """按整条服务配置构造实例的工厂，构造形状完全由扩展自己决定。

    :param conf: 该实例的用户配置
    :return: 下载器客户端桩
    """
    return _DemoDownloader(name=conf.name, host=(conf.config or {}).get("endpoint"))


def _two_argument_factory(conf: Any, extra: Any) -> _DemoDownloader:
    """必填两个参数的工厂，宿主只传配置对象一个位置参数，补不上第二个。"""
    return _DemoDownloader(name=conf.name, host=extra)


def _keyword_only_factory(*, conf: Any) -> _DemoDownloader:
    """只接受关键字参数的工厂，宿主按位置传入的配置对象无处可去。"""
    return _DemoDownloader(name=conf.name)


def _downloader_config(name: str, service_type: str, enabled: bool = True, **config: Any) -> dict:
    """构造一条下载器配置的原始字典。

    :param name: 实例名
    :param service_type: 类型标识
    :param enabled: 是否启用
    :param config: 该实例的配置内容
    :return: 与持久化形状一致的配置字典
    """
    return {"name": name, "type": service_type, "enabled": enabled, "config": config}


@pytest.fixture(autouse=True)
def _isolate_service_instance_registry() -> Iterator[None]:
    """快照并复原服务实例注册表，避免测试间相互污染。"""
    original = dict(service_instance_registry._adapters)
    try:
        yield
    finally:
        service_instance_registry._adapters.clear()
        service_instance_registry._adapters.update(original)


@pytest.fixture
def service_configs() -> Iterator[List[dict]]:
    """接管服务配置读取端口，用例改写列表即改写用户配置。"""
    configs: List[dict] = []
    previous = configure_service_instance_config_reader(
        lambda capability: configs if capability == ModuleType.Downloader.value else None
    )
    try:
        yield configs
    finally:
        configure_service_instance_config_reader(previous)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _CapableServicePlugin:
    """声明服务实例类型的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "服务插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False, render_mode=None):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error
        self._render_mode = render_mode

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_render_mode(self):
        """返回插件渲染模式，未指定时不声明该钩子的语义由基类缺省承担。"""
        return self._render_mode

    def provides_service_instances(self):
        """返回声明的服务实例类型，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明服务实例时出错")
        return self._declarations


def _projection(plugin: Any, key: str = "DemoPlugin") -> PluginProjection:
    """构造只含单个插件桩的能力投影服务。

    :param plugin: 插件桩
    :param key: 实例键
    :return: 能力投影服务
    """
    return PluginProjection({key: plugin})


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
    )
    plugin = _CapableServicePlugin(declarations=[declaration], render_mode=("vuetify", None))

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == [declaration]


@pytest.mark.parametrize(
    ("capability", "impl"),
    [
        ("downloader", _DemoDownloader),
        ("mediaserver", _DemoDownloader),
        ("notification", _DemoNotifier),
    ],
    ids=["downloader", "mediaserver", "notification"],
)
def test_projection_accepts_every_service_capability(capability, impl):
    """三个服务族共用同一条声明，各自的语义标签都应被接受。"""
    plugin = _CapableServicePlugin(
        declarations=[
            ServiceInstanceDeclaration(
                capability=capability,
                type="demo_type",
                name="演示类型",
                impl=impl,
            )
        ],
        render_mode=("vuetify", None),
    )

    declared = _projection(plugin).provided_service_instances()

    assert len(declared["DemoPlugin"]) == 1
    assert declared["DemoPlugin"][0].capability == capability


@pytest.mark.parametrize(
    "capability",
    ["Downloaders", "MediaServers", "Notifications", "storage", "downloaders", "不存在"],
    ids=[
        "systemconfig_downloaders_key",
        "systemconfig_mediaservers_key",
        "systemconfig_notifications_key",
        "capability_outside_service_families",
        "wrong_case",
        "unknown_label",
    ],
)
def test_projection_rejects_capability_outside_the_label_set(capability):
    """标签取值不在集合内的声明必须被拒，宿主存储配置的列表名同样不是合法标签。"""
    plugin = _CapableServicePlugin(
        declarations=[
            ServiceInstanceDeclaration(
                capability=capability,
                type="demo_type",
                name="演示类型",
                impl=_DemoDownloader,
            )
        ],
        render_mode=("vuetify", None),
    )

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == []


@pytest.mark.parametrize(
    "declaration",
    [
        ServiceInstanceDeclaration(
            capability="", type="t", name="N", impl=_DemoDownloader
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="", name="N", impl=_DemoDownloader
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="", impl=_DemoDownloader
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N"
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N",
            impl=_DemoDownloader, factory=_demo_downloader_factory
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", impl="not-a-class"
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", impl=_NoNameDownloader
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", impl=_PositionalOnlyDownloader
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", factory="not-callable"
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", factory=_two_argument_factory
        ),
        ServiceInstanceDeclaration(
            capability="downloader", type="t", name="N", factory=_keyword_only_factory
        ),
    ],
    ids=[
        "capability_missing",
        "type_missing",
        "name_missing",
        "neither_impl_nor_factory",
        "both_impl_and_factory",
        "impl_is_string",
        "impl_rejects_name_keyword",
        "impl_has_positional_only_required_param",
        "factory_not_callable",
        "factory_needs_two_arguments",
        "factory_takes_no_positional_argument",
    ],
)
def test_projection_rejects_declaration_violating_contract(declaration):
    """任一契约项不满足的声明必须整条被拒。"""
    plugin = _CapableServicePlugin(declarations=[declaration], render_mode=("vuetify", None))

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == []


def test_projection_accepts_factory_declaration():
    """只给工厂的声明应被接受，工厂原样保留在声明里。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="factory_downloader",
        name="工厂下载器",
        factory=_demo_downloader_factory,
    )
    plugin = _CapableServicePlugin(declarations=[declaration], render_mode=("vuetify", None))

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == [declaration]
    assert declared["DemoPlugin"][0].factory is _demo_downloader_factory


def test_projection_rejects_bare_impl_class_without_declaration():
    """服务实例类型无法从裸实现类推出能力标签与类型标识，裸类写法必须被拒。"""
    plugin = _CapableServicePlugin(declarations=[_DemoDownloader], render_mode=("vuetify", None))

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == []


def test_projection_rejects_impl_with_unimplemented_abstract_methods():
    """抽象方法未全部落地的实现必须被拒。"""
    from abc import ABC, abstractmethod

    class _AbstractDownloader(ABC):
        """遗漏抽象方法落地的客户端桩。"""

        def __init__(self, name: Optional[str] = None, **kwargs: Any):
            """记录实例名。"""
            self.name = name

        @abstractmethod
        def start(self) -> None:
            """未落地的抽象方法。"""

    plugin = _CapableServicePlugin(
        declarations=[
            ServiceInstanceDeclaration(
                capability="downloader",
                type="abstract_downloader",
                name="抽象下载器",
                impl=_AbstractDownloader,
            )
        ],
        render_mode=("vuetify", None),
    )

    declared = _projection(plugin).provided_service_instances()

    assert declared["DemoPlugin"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableServicePlugin(
        declarations=[
            ServiceInstanceDeclaration(
                capability="downloader", type="ok_type", name="可用", impl=_DemoDownloader
            ),
            ServiceInstanceDeclaration(
                capability="downloader", type="", name="缺类型", impl=_DemoDownloader
            ),
            ServiceInstanceDeclaration(
                capability="downloader", type="bad_type", name="坏实现", impl=_NoNameDownloader
            ),
        ],
        render_mode=("vuetify", None),
    )

    declared = _projection(plugin).provided_service_instances()

    assert [item.type for item in declared["DemoPlugin"]] == ["ok_type"]


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明服务实例抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableServicePlugin(raise_error=True, render_mode=("vuetify", None))
    healthy = _CapableServicePlugin(
        declarations=[
            ServiceInstanceDeclaration(
                capability="downloader", type="ok_type", name="可用", impl=_DemoDownloader
            )
        ],
        render_mode=("vuetify", None),
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_service_instances()

    assert "Broken" not in declared
    assert declared["Ok"][0].type == "ok_type"


def test_projection_skips_disabled_extension():
    """停用的实例不参与服务实例声明投影。"""
    plugin = _CapableServicePlugin(
        enabled=False,
        declarations=[
            ServiceInstanceDeclaration(
                capability="downloader", type="ok_type", name="可用", impl=_DemoDownloader
            )
        ],
        render_mode=("vuetify", None),
    )

    declared = _projection(plugin).provided_service_instances()

    assert declared == {}


class _FakeDownloaderPlugin:
    """声明下载器类型的插件桩，用于驱动插件管理器完整生命周期。"""

    plugin_name = "假想下载器插件"
    plugin_version = "1.0.0"
    service_type = "fake_downloader"
    service_impl = _DemoDownloader

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
        """返回声明的固定下载器类型。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type=self.service_type,
                name="假想下载器",
                impl=self.service_impl,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def _start_plugin(monkeypatch, plugin_manager: PluginManager, plugin_class: type) -> str:
    """按插件类启动一个插件实例并返回其实例键。

    :param monkeypatch: pytest monkeypatch
    :param plugin_manager: 插件管理器
    :param plugin_class: 插件类
    :return: 实例键
    """
    plugin_id = plugin_class.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [plugin_class],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})
    plugin_manager.start(pid=plugin_id)
    return plugin_id


def test_plugin_manager_lifecycle_registers_and_revokes_service_type(
    monkeypatch, plugin_manager: PluginManager
):
    """插件启动后应以实例键为登记方登记声明的服务类型；停止后必须撤销。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    entry = service_instance_registry.find("downloader", "fake_downloader")
    assert entry is not None
    assert entry.owner == plugin_id
    assert entry.distribution == ExtensionDistribution.MARKET
    assert entry.impl is _DemoDownloader
    assert entry.name == "假想下载器"

    plugin_manager.stop(plugin_id)

    assert service_instance_registry.find("downloader", "fake_downloader") is None


def test_plugin_manager_config_update_resyncs_service_registration(
    monkeypatch, plugin_manager: PluginManager
):
    """配置生效后停用实例应撤销登记，重新启用后登记应恢复。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)
    assert service_instance_registry.find("downloader", "fake_downloader") is not None

    plugin_obj = plugin_manager._running_plugins[plugin_id]
    plugin_obj.enabled = False
    plugin_manager.init_plugin(plugin_id, {})
    assert service_instance_registry.find("downloader", "fake_downloader") is None

    plugin_obj.enabled = True
    plugin_manager.init_plugin(plugin_id, {})
    assert service_instance_registry.find("downloader", "fake_downloader").owner == plugin_id


def test_plugin_manager_start_survives_declaration_exception(
    monkeypatch, plugin_manager: PluginManager
):
    """插件的 provides_service_instances 抛异常时不应阻断插件加载。"""

    class _BrokenServicePlugin(_FakeDownloaderPlugin):
        """声明服务实例时抛异常的插件桩。"""

        def provides_service_instances(self):
            """模拟插件实现出错。"""
            raise RuntimeError("声明服务实例时出错")

    plugin_id = _start_plugin(monkeypatch, plugin_manager, _BrokenServicePlugin)

    assert plugin_id in plugin_manager._running_plugins
    assert service_instance_registry.find("downloader", "fake_downloader") is None


def test_declared_downloader_shows_up_in_downloader_helper_services(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """插件声明的下载器类型必须真的出现在 DownloaderHelper.get_services() 里。"""
    service_configs.append(_downloader_config("我的下载器", "fake_downloader", host="127.0.0.1"))
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    services = DownloaderHelper().get_services()

    assert "我的下载器" in services
    service = services["我的下载器"]
    assert service.type == "fake_downloader"
    assert isinstance(service.instance, _DemoDownloader)
    assert service.instance.name == "我的下载器"
    assert service.instance.host == "127.0.0.1"
    assert DownloaderHelper().get_service("我的下载器") is not None
    assert DownloaderHelper().is_downloader("fake_downloader", name="我的下载器")

    plugin_manager.stop(plugin_id)

    assert "我的下载器" not in DownloaderHelper().get_services()


class _FactoryDownloaderPlugin(_FakeDownloaderPlugin):
    """改走工厂路径声明下载器类型的插件桩。"""

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """声明的类型不带实现类，构造交给工厂。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type=self.service_type,
                name="工厂下载器",
                factory=_demo_downloader_factory,
            )
        ]


def test_factory_declaration_builds_instances_from_whole_config(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """工厂路径拿到的是整条配置对象，实例照常出现在服务列表里。"""
    service_configs.append(
        _downloader_config("工厂实例", "fake_downloader", endpoint="10.0.0.1")
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _FactoryDownloaderPlugin)

    services = DownloaderHelper().get_services()

    assert "工厂实例" in services
    instance = services["工厂实例"].instance
    assert isinstance(instance, _DemoDownloader)
    assert instance.name == "工厂实例"
    # 宿主不认识 endpoint 这个配置项，映射到 host 的是工厂而不是宿主的关键字展开
    assert instance.host == "10.0.0.1"
    assert instance.extra == {}

    plugin_manager.stop(plugin_id)

    assert "工厂实例" not in DownloaderHelper().get_services()


def test_declared_downloader_fans_out_one_instance_per_config(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """同一类型下配置几条实例就应产出几个具名实例，停用的配置不产出。"""
    service_configs.extend([
        _downloader_config("下载器甲", "fake_downloader", host="a"),
        _downloader_config("下载器乙", "fake_downloader", host="b"),
        _downloader_config("下载器丙", "fake_downloader", enabled=False, host="c"),
        _downloader_config("别族实例", "other_downloader", host="d"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    services = DownloaderHelper().get_services()

    assert sorted(services) == ["下载器乙", "下载器甲"]
    assert services["下载器甲"].instance.host == "a"
    assert services["下载器乙"].instance.host == "b"


def test_broken_config_does_not_take_down_its_siblings(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """单条配置构造失败只跳过它自己，同类型下其余配置照常产出。"""

    class _ExplodingDownloaderPlugin(_FakeDownloaderPlugin):
        """声明构造可能失败的下载器类型的插件桩。"""

        service_impl = _ExplodingDownloader

    service_configs.extend([
        _downloader_config("好实例", "fake_downloader", host="ok"),
        _downloader_config("坏实例", "fake_downloader", broken=True),
        _downloader_config("另一好实例", "fake_downloader", host="ok2"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _ExplodingDownloaderPlugin)

    services = DownloaderHelper().get_services()

    assert sorted(services) == ["另一好实例", "好实例"]


def test_instances_are_reused_until_their_config_changes(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """配置未变时复用已构造的实例，配置变更后重建，配置消失后摘除。"""
    service_configs.append(_downloader_config("我的下载器", "fake_downloader", host="a"))
    _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)
    helper = DownloaderHelper()

    first = helper.get_services()["我的下载器"].instance
    assert helper.get_services()["我的下载器"].instance is first

    service_configs[0]["config"] = {"host": "b"}
    rebuilt = helper.get_services()["我的下载器"].instance
    assert rebuilt is not first
    assert rebuilt.host == "b"

    service_configs.clear()
    assert helper.get_services() == {}


class _FakeBuiltinModuleManager:
    """只产出给定实例持有者的模块管理器桩，用于隔离内建与扩展的优先级。"""

    def __init__(self, builtin_holders: Iterable[Any]):
        self._builtin_holders = tuple(builtin_holders)

    def get_service_config_modules(self, config_key: str):
        """先产出内建持有者，再产出扩展声明的适配器，与真实实现同序。"""
        yield from self._builtin_holders
        yield from service_instance_registry.adapters(service_capability(config_key))


class _BuiltinDownloaderHolder:
    """持有固定实例的内建模块桩。"""

    def __init__(self, instances: Dict[str, Any]):
        self._instances = instances

    def get_instances(self) -> Dict[str, Any]:
        """返回内建模块持有的实例。"""
        return self._instances


def test_builtin_instances_still_reachable_alongside_declared_types(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """扩展声明接入后，内建模块持有的实例仍照常出现在服务列表里。"""
    builtin_instance = object()
    service_configs.extend([
        _downloader_config("内建实例", "qbittorrent"),
        _downloader_config("扩展实例", "fake_downloader"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)
    helper = DownloaderHelper()
    helper.modulemanager = _FakeBuiltinModuleManager(
        [_BuiltinDownloaderHolder({"内建实例": builtin_instance})]
    )

    services = helper.get_services()

    assert services["内建实例"].instance is builtin_instance
    assert services["内建实例"].type == "qbittorrent"
    assert isinstance(services["扩展实例"].instance, _DemoDownloader)


def test_declared_type_overrides_builtin_type_consistently(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """扩展声明的类型与内建类型同名时覆盖内建实例，且单查与列举结论一致。"""

    class _OverridingDownloaderPlugin(_FakeDownloaderPlugin):
        """声明与内建同名类型的插件桩。"""

        service_type = "qbittorrent"

    builtin_instance = object()
    service_configs.append(_downloader_config("共享实例", "qbittorrent", host="x"))
    _start_plugin(monkeypatch, plugin_manager, _OverridingDownloaderPlugin)
    helper = DownloaderHelper()
    helper.modulemanager = _FakeBuiltinModuleManager(
        [_BuiltinDownloaderHolder({"共享实例": builtin_instance})]
    )

    listed = helper.get_services()["共享实例"].instance
    fetched = helper.get_service("共享实例").instance

    assert isinstance(listed, _DemoDownloader)
    assert fetched is listed


def test_module_manager_yields_declared_adapters_after_builtin_modules(
    monkeypatch, plugin_manager: PluginManager
):
    """真实模块管理器应把扩展声明的适配器接在内建模块之后一并产出。"""
    _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    holders = list(ModuleManager().get_service_config_modules("Downloaders"))

    adapters = service_instance_registry.adapters("downloader")
    assert adapters
    assert holders[-len(adapters):] == list(adapters)
    assert all(hasattr(holder, "get_instances") for holder in holders)


def test_registry_keeps_adapter_when_registration_is_unchanged():
    """内容相同的重复登记应保留原适配器，已构造的实例不因此全部重建。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="stable_type",
        name="稳定类型",
        impl=_DemoDownloader,
        owner="Owner@default",
    )
    first = service_instance_registry.adapters("downloader")[0]

    service_instance_registry.register(
        capability="downloader",
        service_type="stable_type",
        name="稳定类型",
        impl=_DemoDownloader,
        owner="Owner@default",
    )

    assert service_instance_registry.adapters("downloader")[0] is first


def test_registry_revokes_only_its_own_registration():
    """类型被另一个登记方接管后，原登记方的回收不得波及新登记方。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="shared_type",
        name="共享类型",
        impl=_DemoDownloader,
        owner="First@default",
    )
    service_instance_registry.register(
        capability="downloader",
        service_type="shared_type",
        name="共享类型",
        impl=_ExplodingDownloader,
        owner="Second@default",
    )

    service_instance_registry.unregister_owner("First@default")

    entry = service_instance_registry.find("downloader", "shared_type")
    assert entry is not None
    assert entry.owner == "Second@default"


def test_registry_isolates_types_across_capabilities():
    """同名类型登记在不同能力标签下互不干扰。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="same_name",
        name="下载器",
        impl=_DemoDownloader,
        owner="Owner@default",
    )
    service_instance_registry.register(
        capability="notification",
        service_type="same_name",
        name="通知渠道",
        impl=_DemoDownloader,
        owner="Owner@default",
    )

    assert service_instance_registry.find("downloader", "same_name").name == "下载器"
    assert service_instance_registry.find("notification", "same_name").name == "通知渠道"
    assert len(service_instance_registry.adapters("downloader")) == 1


class _RecordingLogger:
    """记录告警与错误文本的日志端口替身。"""

    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def warning(self, message: str) -> None:
        """记录一条告警。"""
        self.warnings.append(message)

    def error(self, message: str) -> None:
        """记录一条错误。"""
        self.errors.append(message)

    def info(self, message: str) -> None:
        """记录一条信息，用例不关心其内容。"""


@pytest.fixture(autouse=True)
def _clean_extension_scoped_warnings() -> Iterator[None]:
    """每个用例前后都清空扩展级去重告警记录，避免用例间互相掩盖。"""
    extension_scoped._extension_scoped_warnings_seen.clear()
    yield
    extension_scoped._extension_scoped_warnings_seen.clear()


def _service_declaration(service_type: str, name: str = "演示下载器") -> ServiceInstanceDeclaration:
    """构造一条契约合规的下载器类型声明。

    :param service_type: 类型标识
    :param name: 类型展示名称
    :return: 服务实例声明
    """
    return ServiceInstanceDeclaration(
        capability="downloader",
        type=service_type,
        name=name,
        impl=_DemoDownloader,
    )


def _sibling_projection(
    declarations_by_key: Dict[str, List[ServiceInstanceDeclaration]],
    log: Any = None,
) -> PluginProjection:
    """构造由多个实例组成的能力投影服务。

    :param declarations_by_key: 实例键到该实例声明列表的映射，字典顺序即登记顺序
    :param log: 日志端口
    :return: 能力投影服务
    """
    running = {
        key: _CapableServicePlugin(
            declarations=declarations, render_mode=("vuetify", None)
        )
        for key, declarations in declarations_by_key.items()
    }
    return PluginProjection(running, log) if log else PluginProjection(running)


def test_sibling_instances_declaring_one_type_register_it_once():
    """同插件两个实例声明同一类型时只认一次，归属默认实例。"""
    projection = _sibling_projection({
        "DemoPlugin": [_service_declaration("my_qb")],
        "DemoPlugin@home": [_service_declaration("my_qb")],
    })

    declared = projection.provided_service_instances()

    assert [item.type for item in declared["DemoPlugin"]] == ["my_qb"]
    assert declared["DemoPlugin@home"] == []


def test_sibling_election_does_not_depend_on_registration_order():
    """认哪一个实例与登记顺序无关，非默认实例先登记同样由默认实例胜出。"""
    projection = _sibling_projection({
        "DemoPlugin@home": [_service_declaration("my_qb")],
        "DemoPlugin": [_service_declaration("my_qb")],
    })

    declared = projection.provided_service_instances()

    assert [item.type for item in declared["DemoPlugin"]] == ["my_qb"]
    assert declared["DemoPlugin@home"] == []


def test_sibling_election_falls_back_to_ascending_instance_id():
    """没有默认实例时按实例标识升序取第一个。"""
    projection = _sibling_projection({
        "DemoPlugin@work": [_service_declaration("my_qb")],
        "DemoPlugin@home": [_service_declaration("my_qb")],
    })

    declared = projection.provided_service_instances()

    assert [item.type for item in declared["DemoPlugin@home"]] == ["my_qb"]
    assert declared["DemoPlugin@work"] == []


def test_duplicate_type_across_siblings_warns_once():
    """同标识重复声明只告警一次，且文案说明这是扩展级声明。"""
    log = _RecordingLogger()
    projection = _sibling_projection(
        {
            "DemoPlugin": [_service_declaration("my_qb")],
            "DemoPlugin@home": [_service_declaration("my_qb")],
        },
        log=log,
    )

    projection.provided_service_instances()
    projection.provided_service_instances()

    assert len(log.warnings) == 1
    message = log.warnings[0]
    assert "downloader/my_qb" in message
    assert "扩展级事实" in message
    assert "只登记一次" in message
    assert "DemoPlugin@home" in message


def test_distinct_types_from_each_sibling_are_all_kept():
    """不同实例声明不同类型是合法的，各自登记且不告警。"""
    log = _RecordingLogger()
    projection = _sibling_projection(
        {
            "DemoPlugin": [_service_declaration("my_qb")],
            "DemoPlugin@home": [_service_declaration("my_tr")],
        },
        log=log,
    )

    declared = projection.provided_service_instances()

    assert [item.type for item in declared["DemoPlugin"]] == ["my_qb"]
    assert [item.type for item in declared["DemoPlugin@home"]] == ["my_tr"]
    assert log.warnings == []


def test_same_type_from_different_plugins_is_not_deduplicated():
    """去重只在同一插件的实例之间进行，插件之间的同名类型仍按覆盖规则处理。"""
    log = _RecordingLogger()
    projection = _sibling_projection(
        {
            "FirstPlugin": [_service_declaration("shared_type")],
            "SecondPlugin": [_service_declaration("shared_type")],
        },
        log=log,
    )

    declared = projection.provided_service_instances()

    assert [item.type for item in declared["FirstPlugin"]] == ["shared_type"]
    assert [item.type for item in declared["SecondPlugin"]] == ["shared_type"]
    assert log.warnings == []


def test_querying_one_instance_key_still_honours_the_family_wide_election():
    """按实例键查询也返回族内裁决后的结果，不因筛选条件变出另一个赢家。"""
    projection = _sibling_projection({
        "DemoPlugin": [_service_declaration("my_qb")],
        "DemoPlugin@home": [_service_declaration("my_qb")],
    })

    assert projection.provided_service_instances("DemoPlugin@home") == {
        "DemoPlugin@home": []
    }
    assert [
        item.type
        for item in projection.provided_service_instances("DemoPlugin")["DemoPlugin"]
    ] == ["my_qb"]


def test_disabled_winner_hands_the_type_to_its_running_sibling():
    """默认实例停用后类型归属仍在运行的兄弟实例，不随停用一起消失。"""
    running = {
        "DemoPlugin": _CapableServicePlugin(
            enabled=False,
            declarations=[_service_declaration("my_qb")],
            render_mode=("vuetify", None),
        ),
        "DemoPlugin@home": _CapableServicePlugin(
            declarations=[_service_declaration("my_qb")],
            render_mode=("vuetify", None),
        ),
    }

    declared = PluginProjection(running).provided_service_instances()

    assert "DemoPlugin" not in declared
    assert [item.type for item in declared["DemoPlugin@home"]] == ["my_qb"]


class _FanOutDownloaderPlugin(_FakeDownloaderPlugin):
    """全部实例都声明同一下载器类型的插件桩，用于驱动多实例生命周期。"""

    plugin_name = "扇出下载器插件"
    service_type = "fanout_downloader"


def _start_instances(
    monkeypatch, plugin_manager: PluginManager, plugin_class: type, instance_ids: List[str]
) -> str:
    """按给定实例清单启动一个插件的多个实例。

    :param monkeypatch: pytest monkeypatch
    :param plugin_manager: 插件管理器
    :param plugin_class: 插件类
    :param instance_ids: 实例标识清单
    :return: 插件ID
    """
    plugin_id = plugin_class.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [plugin_class],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})
    monkeypatch.setattr(plugin_manager, "_plugin_instance_ids", lambda pid: instance_ids)
    plugin_manager.start(pid=plugin_id)
    return plugin_id


def test_sibling_instances_register_the_type_once_with_a_single_owner(
    monkeypatch, plugin_manager: PluginManager
):
    """两个实例声明同一类型时注册表只有一条登记，归属默认实例。"""
    plugin_id = _start_instances(
        monkeypatch, plugin_manager, _FanOutDownloaderPlugin, ["default", "home"]
    )

    assert f"{plugin_id}@home" in plugin_manager._running_plugins
    assert len(service_instance_registry.adapters("downloader")) == 1
    assert service_instance_registry.find("downloader", "fanout_downloader").owner == plugin_id


def test_stopping_the_owner_hands_the_type_to_the_surviving_sibling(
    monkeypatch, plugin_manager: PluginManager
):
    """登记方停止后类型由仍在运行的兄弟实例接手，不随一个实例的停止消失。"""
    plugin_id = _start_instances(
        monkeypatch, plugin_manager, _FanOutDownloaderPlugin, ["default", "home"]
    )

    plugin_manager.stop(plugin_id, instance_id="default")

    entry = service_instance_registry.find("downloader", "fanout_downloader")
    assert entry is not None
    assert entry.owner == f"{plugin_id}@home"


def test_type_is_recycled_only_after_every_sibling_stops(
    monkeypatch, plugin_manager: PluginManager
):
    """全部实例停用后该类型必须被回收干净，注册表不留任何残留。"""
    plugin_id = _start_instances(
        monkeypatch, plugin_manager, _FanOutDownloaderPlugin, ["default", "home"]
    )

    plugin_manager.stop(plugin_id, instance_id="default")
    assert service_instance_registry.find("downloader", "fanout_downloader") is not None

    plugin_manager.stop(plugin_id, instance_id="home")

    assert service_instance_registry.find("downloader", "fanout_downloader") is None
    assert service_instance_registry.adapters("downloader") == ()


def test_stopping_the_whole_plugin_recycles_the_type(
    monkeypatch, plugin_manager: PluginManager
):
    """整插件停止时全部实例的登记一并回收。"""
    plugin_id = _start_instances(
        monkeypatch, plugin_manager, _FanOutDownloaderPlugin, ["default", "home"]
    )

    plugin_manager.stop(plugin_id)

    assert service_instance_registry.find("downloader", "fanout_downloader") is None
    assert service_instance_registry.adapters("downloader") == ()


def test_declared_type_still_serves_configs_after_the_owner_changes(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """归属改选后用户配置的实例照常扇出，取用端感知不到归属变化。"""
    service_configs.append(_downloader_config("我的下载器", "fanout_downloader", host="127.0.0.1"))
    plugin_id = _start_instances(
        monkeypatch, plugin_manager, _FanOutDownloaderPlugin, ["default", "home"]
    )

    plugin_manager.stop(plugin_id, instance_id="default")
    services = DownloaderHelper().get_services()

    assert "我的下载器" in services
    assert isinstance(services["我的下载器"].instance, _DemoDownloader)


def test_declaring_plugin_reads_back_its_own_instances(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """声明方按自己的实例键取回本类型下宿主已构造的具名实例。"""
    service_configs.extend([
        _downloader_config("下载器甲", "fake_downloader", host="a"),
        _downloader_config("下载器乙", "fake_downloader", host="b"),
    ])
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    instances = declared_service_instances("downloader", "fake_downloader", plugin_id)

    assert sorted(instances) == ["下载器乙", "下载器甲"]
    assert instances["下载器甲"].host == "a"
    assert instances["下载器甲"] is DownloaderHelper().get_services()["下载器甲"].instance


def test_reading_back_instances_requires_matching_ownership(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """归属、类型或能力标签对不上时交空表，不退而求其次挑一个。"""
    service_configs.append(_downloader_config("下载器甲", "fake_downloader", host="a"))
    _start_plugin(monkeypatch, plugin_manager, _FakeDownloaderPlugin)

    assert declared_service_instances("downloader", "fake_downloader", "别的插件") == {}
    assert declared_service_instances("downloader", "other_downloader", "_FakeDownloaderPlugin") == {}
    assert declared_service_instances("storage", "fake_downloader", "_FakeDownloaderPlugin") == {}
    assert declared_service_instances("downloader", "fake_downloader", "") == {}
