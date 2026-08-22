"""插件管理器数据库生命周期挂接点测试：建库/释放/销毁的调用时机。

用替身接管 _configure_plugin_database_lifecycle 装配的钩子，不依赖 app.db.plugin
真正建库，只验证 plugin_manager.py 在什么时机调用了什么钩子——这正是「停止只释放
连接、绝不销毁库文件，销毁只在删除插件数据的路径触发」这条不可逆操作边界的回归测试。
"""

from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.extensions.plugin_manager import (
    PluginManager,
    _configure_plugin_database_lifecycle,
)


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


class _FakeDbPlugin:
    """驱动插件管理器完整生命周期的最小插件桩。"""

    plugin_name = "假想数据库插件"
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

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def _install_fake_plugin(monkeypatch, manager: PluginManager, plugin_id: str) -> None:
    """把假想插件接入到指定管理器实例的加载路径。"""
    monkeypatch.setattr(
        manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeDbPlugin],
    )
    monkeypatch.setattr(manager, "get_plugin_config", lambda pid: {})


def test_start_calls_ensure_hook_with_default_instance(monkeypatch, plugin_manager):
    """插件初始化后应调用建库钩子，实例标识固定取默认实例。"""
    plugin_id = _FakeDbPlugin.__name__
    _install_fake_plugin(monkeypatch, plugin_manager, plugin_id)
    calls = []
    _configure_plugin_database_lifecycle(
        ensure=lambda pid_, iid: calls.append(("ensure", pid_, iid)),
        release=lambda pid_: None,
        destroy=lambda pid_, iid: None,
    )

    plugin_manager.start(pid=plugin_id)

    assert ("ensure", plugin_id, DEFAULT_INSTANCE_ID) in calls


def test_stop_calls_release_hook_but_never_destroy(monkeypatch, plugin_manager):
    """
    停止插件只应触发连接释放，绝不能触发库文件销毁。

    stop() 承载了移除单个插件、热重载和整体重启三条路径，任何一条误触发销毁
    都会造成不可逆的数据丢失。
    """
    plugin_id = _FakeDbPlugin.__name__
    _install_fake_plugin(monkeypatch, plugin_manager, plugin_id)
    calls = []
    _configure_plugin_database_lifecycle(
        ensure=lambda pid_, iid: None,
        release=lambda pid_: calls.append(("release", pid_)),
        destroy=lambda pid_, iid: calls.append(("destroy", pid_, iid)),
    )
    plugin_manager.start(pid=plugin_id)
    calls.clear()

    plugin_manager.stop(plugin_id)

    assert ("release", plugin_id) in calls
    assert not any(call[0] == "destroy" for call in calls), "stop() 绝不能触发库文件销毁"


def test_remove_plugin_delegates_to_stop_and_never_destroys(monkeypatch, plugin_manager):
    """remove_plugin 只是 stop 的别名，同样绝不能触发库文件销毁。"""
    plugin_id = _FakeDbPlugin.__name__
    _install_fake_plugin(monkeypatch, plugin_manager, plugin_id)
    calls = []
    _configure_plugin_database_lifecycle(
        ensure=lambda pid_, iid: None,
        release=lambda pid_: calls.append(("release", pid_)),
        destroy=lambda pid_, iid: calls.append(("destroy", pid_, iid)),
    )
    plugin_manager.start(pid=plugin_id)
    calls.clear()

    plugin_manager.remove_plugin(plugin_id)

    assert ("release", plugin_id) in calls
    assert not any(call[0] == "destroy" for call in calls)


def test_delete_plugin_data_calls_destroy_hook(monkeypatch, plugin_manager):
    """删除插件数据必须触发库文件销毁，这是唯一合法的销毁入口。"""
    plugin_id = _FakeDbPlugin.__name__
    _install_fake_plugin(monkeypatch, plugin_manager, plugin_id)
    calls = []
    _configure_plugin_database_lifecycle(
        ensure=lambda pid_, iid: None,
        release=lambda pid_: None,
        destroy=lambda pid_, iid: calls.append(("destroy", pid_, iid)),
    )
    plugin_manager.start(pid=plugin_id)

    result = plugin_manager.delete_plugin_data(plugin_id, force=True)

    assert result is True
    assert calls == [("destroy", plugin_id, DEFAULT_INSTANCE_ID)]


def test_default_ports_are_safe_noops():
    """未装配插件数据库框架时，三个端口都应是安全的空操作，不抛异常。"""
    plugin_manager_module._plugin_database_ensure("SomePlugin", DEFAULT_INSTANCE_ID)
    plugin_manager_module._plugin_database_release("SomePlugin")
    plugin_manager_module._plugin_database_destroy("SomePlugin", DEFAULT_INSTANCE_ID)


def test_configure_plugin_database_lifecycle_replaces_all_three_ports():
    """_configure_plugin_database_lifecycle 必须一次性替换建库、释放与销毁三个端口。"""
    calls = []
    _configure_plugin_database_lifecycle(
        ensure=lambda pid_, iid: calls.append(("ensure", pid_, iid)),
        release=lambda pid_: calls.append(("release", pid_)),
        destroy=lambda pid_, iid: calls.append(("destroy", pid_, iid)),
    )

    plugin_manager_module._plugin_database_ensure("P", DEFAULT_INSTANCE_ID)
    plugin_manager_module._plugin_database_release("P")
    plugin_manager_module._plugin_database_destroy("P", DEFAULT_INSTANCE_ID)

    assert calls == [
        ("ensure", "P", DEFAULT_INSTANCE_ID),
        ("release", "P"),
        ("destroy", "P", DEFAULT_INSTANCE_ID),
    ]
