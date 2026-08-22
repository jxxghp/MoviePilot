"""命令词来源端点：三层可辨、两类失效可见、列举确定与响应模型穿透。

运行期命令表是内建、插件、单独注册三处来源合并出的一张平表，合并完就看不出哪条命令
归谁；插件声明因跨插件同词而双废、或因与内建同名却未声明接管意图而被拒时，用户只会
看到自己敲的命令没反应或执行了别的东西，原因此前只落在服务端日志里。本文件锁住这两件
事在端点上说得清楚。
"""

import inspect
import threading
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import patch

import pytest

from app.api.deps import get_current_active_superuser
from app.api.endpoints import command as command_endpoint
from app.runtime.command import Command
from app.runtime.extensions.admission.command_arbitration import (
    BUILTIN_LAYER,
    OTHER_LAYER,
    PLUGIN_LAYER,
    BuiltinCommandArbiter,
)
from app.runtime.extensions.projection.command import PluginCommandTable
from app.runtime.extensions.registry.command import plugin_command_registry
from app.schemas.command import CommandOrigin


@pytest.fixture(autouse=True)
def _isolate_command_registry() -> Iterator[None]:
    """清空并复原插件命令注册表，避免测试间相互污染。"""
    original = dict(plugin_command_registry._commands)
    plugin_command_registry.clear()
    try:
        yield
    finally:
        plugin_command_registry.clear()
        plugin_command_registry._commands.update(original)


def _chain() -> Command:
    """构造只挂内建命令与插件命令注册表的命令中枢测试对象。

    :return: 命令中枢测试对象
    """
    chain = object.__new__(Command)
    chain._preset_commands = {
        "/version": {
            "func": lambda: None,
            "description": "当前版本",
            "category": "管理",
            "data": {},
        }
    }
    chain._plugin_table = PluginCommandTable(
        builtin_command_words=lambda: chain._preset_commands,
        event_sender=Command.send_plugin_event,
        arbiter=BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None)),
    )
    chain._other_commands = {}
    chain._commands = {}
    chain._rlock = threading.RLock()
    return chain


def _register(cmd: str, pid: str = "AcmePlugin", **kwargs) -> None:
    """登记一条插件命令。

    :param cmd: 命令词
    :param pid: 声明方实例键
    :param kwargs: 覆盖默认字段的取值
    :return: 无返回值
    """
    definition = {
        "cmd": cmd,
        "desc": "插件版本",
        "category": "插件",
        "show": True,
        "data": {},
        "impl": print,
        "overrides_builtin": False,
        "pid": pid,
    }
    definition.update(kwargs)
    existing = list(plugin_command_registry._commands.get(pid, {}).items())
    plugin_command_registry.register(pid, [*existing, (cmd, definition)])


def _origin(origins: List[CommandOrigin], cmd: str) -> CommandOrigin:
    """按命令词取出一条来源条目。

    :param origins: 来源条目列表
    :param cmd: 命令词
    :return: 该命令词的来源条目
    """
    return next(origin for origin in origins if origin.cmd == cmd)


def _endpoint(chain: Command, func) -> Any:
    """经应用层门面调用端点，取数路径与运行期一致。

    :param chain: 命令中枢测试对象
    :param func: 端点函数
    :return: 端点返回值
    """
    with patch(
        "app.application.commands.get_command_object", return_value=chain
    ):
        return func(_="token")


def test_builtin_command_reports_the_builtin_layer():
    """未被任何插件接管的命令来自内建层。"""
    entry = _origin(_chain().command_origins(), "/version")

    assert entry.effective is True
    assert entry.source.layer == BUILTIN_LAYER
    assert entry.source.owner is None
    assert entry.source.description == "当前版本"
    assert entry.shadowed == []
    assert entry.declined == []


def test_plugin_command_reports_which_plugin_instance_it_came_from():
    """插件带来的命令要指出是哪个插件的哪个分身，用户才知道该去停用谁。"""
    _register("/acme_sync", pid="AcmePlugin@alt")

    entry = _origin(_chain().command_origins(), "/acme_sync")

    assert entry.effective is True
    assert entry.source.layer == PLUGIN_LAYER
    assert entry.source.owner == "AcmePlugin@alt"
    assert entry.source.extension_id == "AcmePlugin"
    assert entry.source.instance_id == "alt"


def test_overriding_plugin_command_shows_the_builtin_as_shadowed():
    """插件接管内建命令后，被压住的内建层要交出来，用户才知道命令归谁了。"""
    _register("/version", overrides_builtin=True)

    entry = _origin(_chain().command_origins(), "/version")

    assert entry.source.layer == PLUGIN_LAYER
    assert entry.source.owner == "AcmePlugin"
    assert [layer.layer for layer in entry.shadowed] == [BUILTIN_LAYER]
    assert entry.shadowed[0].description == "当前版本"
    assert entry.declined == []


def test_declined_plugin_command_is_visible_while_the_builtin_stays_effective():
    """未声明接管意图的同名插件命令要以「被拒」的身份出现，而不是凭空消失。"""
    _register("/version")

    entry = _origin(_chain().command_origins(), "/version")

    assert entry.effective is True
    assert entry.source.layer == BUILTIN_LAYER
    assert entry.shadowed == []
    assert [layer.owner for layer in entry.declined] == ["AcmePlugin"]
    assert entry.declined[0].description == "插件版本"
    assert entry.conflict is None


def test_cross_plugin_conflict_is_visible_with_the_plugins_involved():
    """跨插件双废的命令词要交出涉及哪些插件，用户据此知道该让谁改词。"""
    _register("/sync", pid="AlphaPlugin")
    _register("/sync", pid="BetaPlugin")

    entry = _origin(_chain().command_origins(), "/sync")

    assert entry.effective is False
    assert entry.source is None
    assert entry.conflict.plugins == ["AlphaPlugin", "BetaPlugin"]
    assert entry.conflict.owners == ["AlphaPlugin", "BetaPlugin"]


def test_conflicted_builtin_word_still_reports_the_builtin_as_effective():
    """争的若是内建命令词，双废后该词回落为内建，冲突详情一并交出。"""
    _register("/version", pid="AlphaPlugin", overrides_builtin=True)
    _register("/version", pid="BetaPlugin", overrides_builtin=True)

    entry = _origin(_chain().command_origins(), "/version")

    assert entry.effective is True
    assert entry.source.layer == BUILTIN_LAYER
    assert entry.conflict.plugins == ["AlphaPlugin", "BetaPlugin"]


def test_separately_registered_command_reports_the_other_layer():
    """单独注册的命令压住同名插件命令，来源层如实标注。"""
    _register("/acme_sync")
    chain = _chain()
    chain.register(cmd="/acme_sync", func=print, desc="单独注册")

    entry = _origin(chain.command_origins(), "/acme_sync")

    assert entry.source.layer == OTHER_LAYER
    assert [layer.layer for layer in entry.shadowed] == [PLUGIN_LAYER]


def test_stopping_a_plugin_removes_its_command_from_the_listing():
    """插件停用后其命令不再出现在来源列表里。"""
    _register("/acme_sync")
    chain = _chain()
    assert _origin(chain.command_origins(), "/acme_sync").effective is True

    plugin_command_registry.unregister_owner("AcmePlugin")

    assert all(entry.cmd != "/acme_sync" for entry in chain.command_origins())


def test_listing_order_is_independent_of_registration_order():
    """列举按命令词排序，与插件登记先后无关。"""
    _register("/zulu", pid="AlphaPlugin")
    _register("/alpha", pid="BetaPlugin")

    words = [entry.cmd for entry in _chain().command_origins()]

    assert words == sorted(words)


def test_conflict_endpoint_lists_only_words_with_a_failed_plugin_claim():
    """失效清单只收录插件声明没生效的命令词，两类失效都要在。"""
    _register("/version")
    _register("/sync", pid="AlphaPlugin")
    _register("/sync", pid="BetaPlugin")
    _register("/acme_sync", pid="AcmePlugin@alt")
    chain = _chain()

    words = [entry.cmd for entry in _endpoint(chain, command_endpoint.command_conflicts)]

    assert words == ["/sync", "/version"]


def test_response_model_keeps_every_nested_field_the_endpoint_returns():
    """端点返回的嵌套字段必须全部能穿过响应模型，否则会被 FastAPI 静默裁掉。"""
    _register("/version", pid="AcmePlugin@alt")
    _register("/version", pid="OtherPlugin")

    entry = _origin(_endpoint(_chain(), command_endpoint.command_origins), "/version")
    serialized = CommandOrigin(**entry.model_dump()).model_dump()

    assert set(serialized) == set(entry.model_dump())
    assert serialized["source"]["layer"] == BUILTIN_LAYER
    assert serialized["conflict"]["plugins"] == ["AcmePlugin", "OtherPlugin"]
    assert serialized["conflict"]["owners"] == ["AcmePlugin@alt", "OtherPlugin"]


def test_response_model_keeps_the_declined_plugin_layer():
    """被拒的插件声明是嵌套结构，同样要能整体穿过响应模型。"""
    _register("/version", pid="AcmePlugin@alt")

    entry = _origin(_endpoint(_chain(), command_endpoint.command_origins), "/version")
    serialized = CommandOrigin(**entry.model_dump()).model_dump()

    assert serialized["declined"][0]["owner"] == "AcmePlugin@alt"
    assert serialized["declined"][0]["extension_id"] == "AcmePlugin"
    assert serialized["declined"][0]["instance_id"] == "alt"


def test_command_endpoints_require_superuser():
    """命令表含重启、清缓存这类系统操作，来源信息按设置类端点的口径限管理员。"""
    def dependency(func: Any) -> Any:
        """读取端点参数上声明的依赖函数。"""
        return inspect.signature(func).parameters["_"].default.dependency

    assert dependency(command_endpoint.command_origins) is get_current_active_superuser
    assert dependency(command_endpoint.command_conflicts) is get_current_active_superuser


def test_endpoints_delegate_to_the_application_facade():
    """端点只做转发，取数走应用层门面而不是自己够进命令中枢。"""
    _register("/acme_sync")
    chain = _chain()

    origins = _endpoint(chain, command_endpoint.command_origins)

    assert _origin(origins, "/acme_sync").source.layer == PLUGIN_LAYER
