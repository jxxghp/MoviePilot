from app.foundation.singleton import Singleton
from app.runtime.events import EventManager, eventmanager
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.plugin_manager import PluginManager
from app.sdk.plugins import ModuleManager as SdkModuleManager
from app.sdk.plugins import PluginManager as SdkPluginManager


def _singleton_key(manager_type: type) -> tuple:
    """返回无参数 Singleton 管理器使用的缓存键。"""
    return manager_type, (), frozenset()


def test_event_manager_global_and_constructor_share_identity():
    """事件全局对象与公开构造入口必须指向同一单例。"""
    assert EventManager() is eventmanager
    assert EventManager() is EventManager()


def test_module_manager_sdk_and_runtime_share_identity(monkeypatch):
    """模块管理器 SDK 与运行时入口必须解析到同一单例对象。"""
    instance = object.__new__(ModuleManager)
    instances = dict(Singleton._instances)
    instances[_singleton_key(ModuleManager)] = instance
    monkeypatch.setattr(Singleton, "_instances", instances)

    assert SdkModuleManager is ModuleManager
    assert SdkModuleManager() is instance
    assert ModuleManager() is instance


def test_plugin_manager_sdk_and_runtime_share_identity(monkeypatch):
    """插件管理器 SDK 与运行时入口必须解析到同一单例对象。"""
    instance = object.__new__(PluginManager)
    instances = dict(Singleton._instances)
    instances[_singleton_key(PluginManager)] = instance
    monkeypatch.setattr(Singleton, "_instances", instances)

    assert SdkPluginManager is PluginManager
    assert SdkPluginManager() is instance
    assert PluginManager() is instance
