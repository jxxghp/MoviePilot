"""定向输入事件在源插件本体与其分身之间的投递归属测试。"""

import sys
from types import ModuleType

from app.runtime.event.dispatch import EventDispatcher
from app.runtime.event.registry import EventRegistry


def _install_plugin_handler(
    monkeypatch,
    *,
    module_name: str,
    declared_class_name: str,
    runtime_class_name: str,
):
    """在指定模块命名空间下造出一个插件事件处理器，并返回其注册标识。

    复刻装载器造出的运行事实：分身与本体共享源码，因此处理器的 ``__qualname__``
    恒为源类名；区别只在分身被放进自己的模块命名空间、并被改写了类的 ``__name__``。

    :param monkeypatch: 用于把假模块登记进 ``sys.modules`` 并在用例结束后还原
    :param module_name: 该实例的模块名
    :param declared_class_name: 源码里声明的类名，即处理器限定名的前缀
    :param runtime_class_name: 运行身份的类名，分身为其实例 ID
    :return: 处理器函数与其注册标识
    """
    module = ModuleType(module_name)

    def handle_message_action(self, event):
        """占位的定向输入事件处理器。"""
        return self, event

    handle_message_action.__module__ = module_name
    handle_message_action.__qualname__ = f"{declared_class_name}.handle_message_action"
    plugin_class = type(
        declared_class_name,
        (),
        {"handle_message_action": handle_message_action},
    )
    plugin_class.__module__ = module_name
    plugin_class.__name__ = runtime_class_name
    setattr(module, declared_class_name, plugin_class)
    monkeypatch.setitem(sys.modules, module_name, module)

    handler = plugin_class.handle_message_action
    return handler, EventRegistry.handler_identifier(handler)


def test_targeted_event_reaches_the_clone_that_opened_the_input_session(monkeypatch):
    """定向到分身的输入事件必须投递给该分身自己的处理器。

    分身共享源码，处理器限定名里的类名始终是源类名，只比类名会让「目标是分身」
    这一判断恒不成立，用户在分身里发起的输入会话随后收不到任何回复。
    """
    handler, handler_id = _install_plugin_handler(
        monkeypatch,
        module_name="app.plugins.demopluginwork",
        declared_class_name="DemoPlugin",
        runtime_class_name="DemoPluginWork",
    )

    assert handler_id == "app.plugins.demopluginwork.DemoPlugin.handle_message_action"
    assert EventDispatcher.should_dispatch_to_target_plugin(
        handler,
        handler_id,
        "DemoPluginWork",
    ) is True


def test_targeted_event_for_the_host_is_not_broadcast_to_its_clones(monkeypatch):
    """定向到本体的输入事件不得同时落进它的分身。

    分身与本体的处理器限定名完全一致，只比类名会让本体的定向事件被全部分身一起
    收到——定向投递本来就是为了不让自由文本被别的实例看到。
    """
    host_handler, host_id = _install_plugin_handler(
        monkeypatch,
        module_name="app.plugins.demoplugin",
        declared_class_name="DemoPlugin",
        runtime_class_name="DemoPlugin",
    )
    clone_handler, clone_id = _install_plugin_handler(
        monkeypatch,
        module_name="app.plugins.demopluginwork",
        declared_class_name="DemoPlugin",
        runtime_class_name="DemoPluginWork",
    )

    assert EventDispatcher.should_dispatch_to_target_plugin(
        host_handler,
        host_id,
        "DemoPlugin",
    ) is True
    assert EventDispatcher.should_dispatch_to_target_plugin(
        clone_handler,
        clone_id,
        "DemoPlugin",
    ) is False
    assert EventDispatcher.should_dispatch_to_target_plugin(
        host_handler,
        host_id,
        "DemoPluginWork",
    ) is False


def test_non_plugin_handlers_keep_matching_by_declared_class_name(monkeypatch):
    """宿主侧处理器不在插件命名空间里，仍按声明类名匹配。"""
    handler, handler_id = _install_plugin_handler(
        monkeypatch,
        module_name="tests.hosted.demo_plugin",
        declared_class_name="DemoPlugin",
        runtime_class_name="DemoPlugin",
    )

    assert EventDispatcher.should_dispatch_to_target_plugin(
        handler,
        handler_id,
        "DemoPlugin",
    ) is True
    assert EventDispatcher.should_dispatch_to_target_plugin(
        handler,
        handler_id,
        "OtherPlugin",
    ) is False
