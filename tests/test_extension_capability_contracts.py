"""扩展声明的能力契约测试：能力面即方法表的键、服务实例的族级必填方法。

带方法表的声明不另行声明能力面——宿主本就知道表里挂了哪些方法，让作者再写一遍只会
多出一处能写错、且写错也无人消费的数据；服务实例的必填方法则定在宿主把实例交出去之
后族级取用链上的无保护直调上，判定是「这几个名字必须在场」。

本文件同时锁住两条边界：必填集之外的方法缺席不算违约（缺席即弃权，不必写空桩），
以及能力契约不得改变单播的弃权协议（None 未认领、空列表已认领）。
"""

from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import (
    MediaSourceDeclaration,
    ModuleDeclaration,
    ServiceInstanceDeclaration,
    declaration_methods,
)
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.admission.media_source import (
    media_source_declaration_violation,
)
from app.runtime.extensions.admission.module import module_declaration_violation
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.contract.service_instance import (
    SERVICE_INSTANCE_REQUIRED_METHODS,
    service_instance_shape_violation,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.schemas.types import ModuleType

_TRANSIENT_FAMILY_OWNER = "TransientFamilyOwner"


def _handler() -> str:
    """契约校验用的最小可调用桩。"""
    return "ok"


class _CapableModulePlugin:
    """声明模块方法表的最小插件桩，用于驱动 PluginProjection。"""

    plugin_name = "模块插件"

    def __init__(self, declarations: Optional[List[Any]] = None):
        """保存待声明的模块方法表。"""
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def provides_modules(self) -> Optional[List[Any]]:
        """返回声明的模块方法表。"""
        return self._declarations


class _CapableServicePlugin:
    """声明服务实例类型的最小插件桩，用于驱动 PluginProjection。"""

    plugin_name = "服务插件"

    def __init__(self, declarations: Optional[List[Any]] = None):
        """保存待声明的服务实例类型。"""
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def get_render_mode(self):
        """返回插件渲染模式，本文件的声明都不带 vue 配置组件。"""
        return "vuetify", None

    def provides_service_instances(self) -> Optional[List[Any]]:
        """返回声明的服务实例类型。"""
        return self._declarations


class _MinimalDownloader:
    """只落地下载器族必填方法的客户端桩，业务方法一个都不写。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def is_inactive(self) -> bool:
        """回答连接是否已断开。"""
        return False

    def reconnect(self) -> bool:
        """重建连接。"""
        return True


class _MinimalMediaServer(_MinimalDownloader):
    """只落地媒体服务器族必填方法的服务器桩，必填集与下载器族相同。"""


class _MinimalNotifier:
    """只落地消息通知族必填方法的通道桩，不写 send_msg 之类的业务方法。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def get_state(self) -> bool:
        """回答通道是否就绪。"""
        return True


class _ShapelessClient:
    """一个契约方法都不带的客户端桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name


class _NoReconnectDownloader:
    """缺 reconnect 的下载器桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def is_inactive(self) -> bool:
        """回答连接是否已断开。"""
        return False


class _NoIsInactiveMediaServer:
    """缺 is_inactive 的媒体服务器桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def reconnect(self) -> bool:
        """重建连接。"""
        return True


def _service_declaration(capability: str, impl: Any, service_type: str = "demo_type"):
    """构造一条除实现类外全部合规的服务实例声明。

    :param capability: 能力标签
    :param impl: 实例实现类
    :param service_type: 类型标识
    :return: 服务实例类型声明
    """
    return ServiceInstanceDeclaration(
        capability=capability,
        type=service_type,
        name="演示类型",
        impl=impl,
    )


@pytest.fixture
def transient_service_family() -> Iterator[str]:
    """登记一族运行期新增的服务，用例结束后回收。

    :return: 该族的能力标签
    """
    capability = "demo_family"
    service_family_registry.register(
        capability,
        "演示族",
        owner=_TRANSIENT_FAMILY_OWNER,
        distribution=ExtensionDistribution.MARKET,
    )
    try:
        yield capability
    finally:
        service_family_registry.unregister_owner(_TRANSIENT_FAMILY_OWNER)


# ---------------------------------------------------------------------------
# 能力面即方法表的键
# ---------------------------------------------------------------------------


def test_method_table_alone_answers_what_the_declaration_provides() -> None:
    """只写方法表的声明照常登记，宿主看到的能力面就是表的键集。"""
    declaration = ModuleDeclaration(
        methods={"recognize": _handler, "obtain_images": _handler}
    )

    assert module_declaration_violation(declaration) is None
    assert set(declaration_methods(declaration)) == {"recognize", "obtain_images"}


def test_declaration_carries_no_second_place_to_name_its_methods() -> None:
    """声明面不留第二处写方法名的位置，写了即构造失败而不是被静默忽略。"""
    with pytest.raises(TypeError):
        ModuleDeclaration(methods={"recognize": _handler}, capabilities=["recognize"])


def test_media_source_shares_the_same_method_table_rule() -> None:
    """媒体数据源与模块声明共用同一张方法表判定，同一份畸形方法表两边都被拒。"""
    malformed = {"search_medias": "not-callable"}

    assert module_declaration_violation(ModuleDeclaration(methods=malformed)) is not None
    assert media_source_declaration_violation(
        MediaSourceDeclaration(media_source="demo", name="演示来源", methods=malformed)
    ) is not None


# ---------------------------------------------------------------------------
# 兼容写法：插件直接交出裸方法表字典
# ---------------------------------------------------------------------------


def test_bare_method_table_named_capabilities_is_dispatched_as_a_method() -> None:
    """裸方法表字典里名为 capabilities 的键是一个可被分发的方法。"""
    projection = PluginProjection(
        {"DemoModule": _CapableModulePlugin([{"capabilities": _handler}])}
    )

    assert _dispatcher(projection).unicast("capabilities") == "ok"


def test_bare_method_table_with_non_callable_value_fails_on_the_table_rule() -> None:
    """裸字典里的值不可调用时按方法表规则被拒。"""
    violation = module_declaration_violation({"capabilities": ["recognize"]})

    assert violation is not None
    assert "不可调用" in violation


# ---------------------------------------------------------------------------
# 服务实例的族级必填方法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("capability", "impl", "missing"),
    [
        (ModuleType.Downloader.value, _NoReconnectDownloader, "reconnect"),
        (ModuleType.MediaServer.value, _NoIsInactiveMediaServer, "is_inactive"),
        (ModuleType.Notification.value, _ShapelessClient, "get_state"),
    ],
    ids=["downloader", "mediaserver", "notification"],
)
def test_service_instance_missing_required_method_is_rejected(
    capability, impl, missing
) -> None:
    """三族各缺一个必填方法都被拒，违约描述点名缺的是哪个方法与哪一族。"""
    violation = service_instance_declaration_violation(_service_declaration(capability, impl))

    assert violation is not None
    assert missing in violation
    assert capability in violation


@pytest.mark.parametrize(
    ("capability", "impl"),
    [
        (ModuleType.Downloader.value, _MinimalDownloader),
        (ModuleType.MediaServer.value, _MinimalMediaServer),
        (ModuleType.Notification.value, _MinimalNotifier),
    ],
    ids=["downloader", "mediaserver", "notification"],
)
def test_service_instance_with_only_required_methods_is_accepted(capability, impl) -> None:
    """只落地必填集、可选方法一个不写的实现照常登记：缺席即弃权，不必写空桩。"""
    assert service_instance_declaration_violation(_service_declaration(capability, impl)) is None


def test_auth_family_has_no_required_methods() -> None:
    """登录认证族零内建类型、握手走模块分发，宿主对实例一个方法都不调。"""
    assert ModuleType.Auth.value not in SERVICE_INSTANCE_REQUIRED_METHODS
    assert service_instance_declaration_violation(
        _service_declaration(ModuleType.Auth.value, _ShapelessClient)
    ) is None


def test_storage_family_shape_is_not_judged_twice() -> None:
    """存储族的形状已由存储后端契约判定，必填集里不再重复登记一份。"""
    assert ModuleType.Storage.value not in SERVICE_INSTANCE_REQUIRED_METHODS


def test_runtime_registered_family_has_no_required_methods(transient_service_family) -> None:
    """运行期新登记的族没有必填集，宿主答不出它的形状就不该硬编一份。"""
    assert transient_service_family not in SERVICE_INSTANCE_REQUIRED_METHODS
    assert service_instance_declaration_violation(
        _service_declaration(transient_service_family, _ShapelessClient)
    ) is None


def test_factory_path_is_not_shape_checked() -> None:
    """走工厂路径时宿主拿不到产出类型，形状判定不成立，因此不判。"""
    assert service_instance_shape_violation(ModuleType.Downloader.value, None) is None


def test_non_callable_attribute_does_not_satisfy_the_required_method() -> None:
    """必填名字被占成非可调用属性时不算落地，判定看的是能不能调。"""
    impl = SimpleNamespace(is_inactive=True, reconnect=lambda: True)

    violation = service_instance_shape_violation(ModuleType.Downloader.value, impl)

    assert violation is not None
    assert "is_inactive" in violation


def test_constructor_violation_still_wins_over_shape_violation() -> None:
    """构造签名不成立时按原违约理由被拒，形状判定不抢在它前面。"""

    class _NoNameShapelessDownloader:
        """既不接受关键字 name、也不带必填方法的客户端桩。"""

        def __init__(self, host: str):
            """只接受 host。"""
            self.host = host

    violation = service_instance_declaration_violation(
        _service_declaration(ModuleType.Downloader.value, _NoNameShapelessDownloader)
    )

    assert violation is not None
    assert "name" in violation
    assert "is_inactive" not in violation


# ---------------------------------------------------------------------------
# 逐条错误隔离
# ---------------------------------------------------------------------------


def test_bad_module_method_table_only_skips_its_own_declaration() -> None:
    """方法表不合契约的声明只跳过它自己，同一插件的其余声明照常登记。"""
    good = ModuleDeclaration(methods={"recognize": _handler})
    bad = ModuleDeclaration(methods={"obtain_images": "not-callable"})
    projection = PluginProjection({"DemoModule": _CapableModulePlugin([bad, good])})

    assert projection.provided_modules()["DemoModule"] == [good]


def test_bad_service_instance_shape_only_skips_its_own_declaration() -> None:
    """形状不合契约的服务实例声明只跳过它自己，同一插件的其余声明照常登记。"""
    good = _service_declaration(ModuleType.Downloader.value, _MinimalDownloader, "good_type")
    bad = _service_declaration(ModuleType.Downloader.value, _ShapelessClient, "bad_type")
    projection = PluginProjection({"DemoService": _CapableServicePlugin([bad, good])})

    assert projection.provided_service_instances()["DemoService"] == [good]


# ---------------------------------------------------------------------------
# 弃权协议
# ---------------------------------------------------------------------------


class _PluginCatalog:
    """把插件投影包装成模块调度器消费的插件目录。"""

    def __init__(self, projection: PluginProjection):
        """绑定插件投影。"""
        self._projection = projection

    def get_plugin_modules(self) -> Dict[Any, Any]:
        """返回当前插件模块方法表快照。"""
        return self._projection.modules()


def _dispatcher(projection: PluginProjection) -> ModuleInvocationDispatcher:
    """构造只接插件来源、不接宿主模块的最小调度器。

    :param projection: 插件投影
    :return: 模块调度器
    """

    class _EmptyModuleCatalog:
        """不提供任何宿主模块的空目录。"""

        def get_running_modules(self, _method: str):
            """始终返回空序列。"""
            return []

        def providers_for(self, _method: str):
            """始终返回空序列。"""
            return ()

    return ModuleInvocationDispatcher(
        module_catalog=_EmptyModuleCatalog(),
        plugin_catalog=_PluginCatalog(projection),
        plugin_error_handler=lambda *a, **k: None,
        system_error_handler=lambda *a, **k: None,
        rate_limit_handler=lambda *a, **k: None,
    )


def test_empty_list_answer_still_claims_and_short_circuits_unicast() -> None:
    """声明式方法表返回空列表仍算已认领，单播在此短路。"""
    claiming = _CapableModulePlugin(
        [ModuleDeclaration(methods={"search_medias": lambda: []})]
    )
    later = _CapableModulePlugin(
        [ModuleDeclaration(methods={"search_medias": lambda: ["后来者"]})]
    )
    dispatcher = _dispatcher(PluginProjection({"AClaiming": claiming, "BLater": later}))

    assert dispatcher.unicast("search_medias") == []


def test_none_answer_abstains_and_lets_the_next_provider_answer() -> None:
    """声明式方法表返回 None 才算未认领，单播继续问下一个。"""
    abstaining = _CapableModulePlugin(
        [ModuleDeclaration(methods={"search_medias": lambda: None})]
    )
    later = _CapableModulePlugin(
        [ModuleDeclaration(methods={"search_medias": lambda: ["后来者"]})]
    )
    dispatcher = _dispatcher(PluginProjection({"AAbstaining": abstaining, "BLater": later}))

    assert dispatcher.unicast("search_medias") == ["后来者"]
