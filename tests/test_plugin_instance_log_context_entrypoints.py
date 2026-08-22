"""插件初始化路径与 HTTP API 入口的实例日志归档契约测试。

覆盖两个此前未接入实例日志上下文的入口：
1. 插件实例的构造与 `init_plugin` 生效期间产生的日志（`PluginManager.__start_instance`
   与 `PluginManager.init_plugin`）；
2. 插件通过 `get_api()` 注册的路由被 FastAPI 调用时产生的日志
   （`PluginProjection.apis()` 对 `endpoint` 的实例绑定）。

两处执行期间产生的插件日志都应归入发起它的那个实例目录，而不是落入插件兜底目录
（`log_module.UNATTRIBUTED_INSTANCE_ID`）。

日志模块的路由状态是进程内共享的全局状态，自动生效 fixture 在每个用例前完整
快照/还原，避免污染同一进程内的其余测试（做法与 test_plugin_instance_logging.py
一致）。
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from app.foundation.singleton import Singleton
from app.runtime import log as log_module
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID, instance_key
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager

PLUGIN_ID = "_LogEntrypointPlugin"
SECOND_INSTANCE = "second"
SECOND_KEY = instance_key(PLUGIN_ID, SECOND_INSTANCE)


class _FakeWriter:
    """记录实际写入调用的内存日志写入器替身。"""

    def __init__(self) -> None:
        """初始化空的调用记录列表。"""
        self.calls: list = []

    def write_log(self, level, message, file_path) -> None:
        """记录一次写入调用而不落盘。"""
        self.calls.append((level, message, file_path))

    def shutdown(self) -> None:
        """替身无需释放任何资源。"""


@pytest.fixture(autouse=True)
def _isolate_plugin_log_state(monkeypatch):
    """快照并还原插件日志路由涉及的全部进程内全局状态。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module, "_plugin_level_floor", log_module._current_global_log_level())
    monkeypatch.setattr(log_module, "_plugin_log_dir_resolver", None)
    monkeypatch.setattr(log_module, "_plugin_log_dir_cache", {})
    monkeypatch.setattr(log_module.LoggerManager, "_writer", None)
    monkeypatch.setattr(log_module.LoggerManager, "_log_path", None)
    monkeypatch.setattr(log_module.LoggerManager, "_pending_file_logs", log_module.deque(maxlen=1000))
    yield


@pytest.fixture(name="fake_writer")
def fixture_fake_writer(monkeypatch):
    """装配内存写入器并同步日志基准路径，跳过真实文件 I/O。"""
    writer = _FakeWriter()
    monkeypatch.setattr(log_module.LoggerManager, "_writer", writer)
    monkeypatch.setattr(log_module.LoggerManager, "_log_path", Path("/unused"))
    return writer


@pytest.fixture(name="instance_log_dir_resolver")
def fixture_instance_log_dir_resolver(tmp_path):
    """把插件实例日志目录解析器指向临时目录下的 <插件id>/<实例id>/logs。"""

    def resolver(plugin_id: str, instance_id: str) -> Path:
        return tmp_path / plugin_id / instance_id / "logs"

    log_module.configure_plugin_log_dir_resolver(resolver)
    return resolver


def _messages_by_file(fake_writer: _FakeWriter) -> Dict[Path, List[str]]:
    """把写入器记录的调用按目标文件分组，便于按目录断言归属。"""
    grouped: Dict[Path, List[str]] = {}
    for _level, message, file_path in fake_writer.calls:
        grouped.setdefault(file_path, []).append(message)
    return grouped


# ---------------------------------------------------------------------------
# 1. 插件初始化路径：构造与 init_plugin 期间的日志归入发起它的实例
# ---------------------------------------------------------------------------


class _LogEntrypointPlugin:
    """构造与 init_plugin 期间各写一条日志的最小插件桩，无参构造。"""

    plugin_name = "写日志插件"
    plugin_version = "1.0.0"

    def __init__(self) -> None:
        """构造期间写一条日志，验证实例上下文在构造阶段已经生效。"""
        self.config: dict = {}
        log_module.logger.info("constructed instance")

    def init_plugin(self, config: dict = None) -> None:
        """生效配置期间写一条日志，验证实例上下文覆盖 init_plugin 阶段。"""
        self.config = config or {}
        log_module.logger.info("initialized instance")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self.config.get("enable"))

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name


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


def _install_lifecycle_plugin(
    monkeypatch: pytest.MonkeyPatch,
    manager: PluginManager,
    plugin_class: type,
    instance_ids: List[str],
    configs: Dict[str, dict],
) -> None:
    """把插件桩接入管理器的加载路径，并给定实例清单与各实例配置。"""
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


def test_plugin_construct_and_init_logs_route_to_owning_instance_directory(
    monkeypatch, plugin_manager, fake_writer, instance_log_dir_resolver
):
    """插件构造与 init_plugin 期间产生的日志应落到发起它的实例目录，而非兜底目录。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LogEntrypointPlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {PLUGIN_ID: {"enable": True}, SECOND_KEY: {"enable": False}},
    )

    plugin_manager.start(pid=PLUGIN_ID)

    grouped = _messages_by_file(fake_writer)
    default_dir = instance_log_dir_resolver(PLUGIN_ID, DEFAULT_INSTANCE_ID) / log_module.PLUGIN_LOG_FILENAME
    second_dir = instance_log_dir_resolver(PLUGIN_ID, SECOND_INSTANCE) / log_module.PLUGIN_LOG_FILENAME
    unattributed_dir = (
        instance_log_dir_resolver(PLUGIN_ID, log_module.UNATTRIBUTED_INSTANCE_ID)
        / log_module.PLUGIN_LOG_FILENAME
    )

    assert any("constructed instance" in m for m in grouped.get(default_dir, []))
    assert any("initialized instance" in m for m in grouped.get(default_dir, []))
    assert any("constructed instance" in m for m in grouped.get(second_dir, []))
    assert any("initialized instance" in m for m in grouped.get(second_dir, []))
    assert unattributed_dir not in grouped


def test_plugin_reinit_via_public_init_plugin_routes_to_owning_instance_directory(
    monkeypatch, plugin_manager, fake_writer, instance_log_dir_resolver
):
    """配置页触发的重新生效（公开 init_plugin 方法）同样归入发起它的实例目录。"""
    _install_lifecycle_plugin(
        monkeypatch,
        plugin_manager,
        _LogEntrypointPlugin,
        [DEFAULT_INSTANCE_ID, SECOND_INSTANCE],
        {PLUGIN_ID: {"enable": True}, SECOND_KEY: {"enable": False}},
    )
    plugin_manager.start(pid=PLUGIN_ID)
    fake_writer.calls.clear()

    plugin_manager.init_plugin(PLUGIN_ID, {"enable": True}, instance_id=SECOND_INSTANCE)

    grouped = _messages_by_file(fake_writer)
    second_dir = instance_log_dir_resolver(PLUGIN_ID, SECOND_INSTANCE) / log_module.PLUGIN_LOG_FILENAME
    default_dir = instance_log_dir_resolver(PLUGIN_ID, DEFAULT_INSTANCE_ID) / log_module.PLUGIN_LOG_FILENAME
    unattributed_dir = (
        instance_log_dir_resolver(PLUGIN_ID, log_module.UNATTRIBUTED_INSTANCE_ID)
        / log_module.PLUGIN_LOG_FILENAME
    )

    assert any("initialized instance" in m for m in grouped.get(second_dir, []))
    assert default_dir not in grouped
    assert unattributed_dir not in grouped


# ---------------------------------------------------------------------------
# 2. HTTP API 入口：get_api() 注册的路由被调用时的日志归入声明它的实例
# ---------------------------------------------------------------------------


class _ApiEndpointPlugin:
    """声明一条会写日志的 HTTP API 路由的最小插件桩。"""

    plugin_name = "接口插件"

    def __init__(self, label: str) -> None:
        """记录本实例的可辨识标签，用于断言日志消息的来源。"""
        self._label = label
        self.calls = 0

    def get_state(self) -> bool:
        """插件始终启用。"""
        return True

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def get_api(self) -> List[Dict[str, Any]]:
        """声明一条状态查询路由，endpoint 绑定到本实例的方法。"""
        return [{"path": "/status", "endpoint": self.status, "methods": ["GET"]}]

    def status(self) -> dict:
        """处理状态查询请求期间写一条日志。"""
        self.calls += 1
        log_module.logger.info(f"status called on {self._label}")
        return {"ok": True}


def test_http_api_endpoint_log_routes_to_owning_instance_directory(
    fake_writer, instance_log_dir_resolver
):
    """插件 get_api() 注册的路由被调用时，日志应落到声明该路由的实例目录。

    调用现场刻意不带任何 `bind_plugin_instance` 绑定，模拟 FastAPI 在真实请求到达时
    直接调用路由注册阶段捕获的 endpoint，不经过宿主任何受控调用点。
    """
    default_plugin = _ApiEndpointPlugin("default")
    second_plugin = _ApiEndpointPlugin("second")
    projection = PluginProjection({PLUGIN_ID: default_plugin, SECOND_KEY: second_plugin})

    endpoints_by_path = {api["path"]: api["endpoint"] for api in projection.apis()}

    endpoints_by_path[f"/{PLUGIN_ID}/status"]()
    endpoints_by_path[f"/{SECOND_KEY}/status"]()

    grouped = _messages_by_file(fake_writer)
    default_dir = instance_log_dir_resolver(PLUGIN_ID, DEFAULT_INSTANCE_ID) / log_module.PLUGIN_LOG_FILENAME
    second_dir = instance_log_dir_resolver(PLUGIN_ID, SECOND_INSTANCE) / log_module.PLUGIN_LOG_FILENAME
    unattributed_dir = (
        instance_log_dir_resolver(PLUGIN_ID, log_module.UNATTRIBUTED_INSTANCE_ID)
        / log_module.PLUGIN_LOG_FILENAME
    )

    assert any("status called on default" in m for m in grouped.get(default_dir, []))
    assert any("status called on second" in m for m in grouped.get(second_dir, []))
    assert unattributed_dir not in grouped
    assert default_plugin.calls == 1
    assert second_plugin.calls == 1
