"""插件事件异常处理的回归测试。"""

import pytest

from app.runtime.events import Event, EventManager
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.types import ChainEventType


class FailingDiscoverPlugin:
    """模拟只实现公开名称接口且事件处理失败的插件。"""

    @staticmethod
    def get_name() -> str:
        """返回插件显示名称。"""
        return "测试发现插件"

    @staticmethod
    def handle(_event: Event) -> None:
        """模拟插件事件处理失败。"""
        raise RuntimeError("discover failed")


@pytest.mark.asyncio
async def test_plugin_event_error_uses_public_display_name(monkeypatch):
    """同步和异步调度都应通过 get_name 获取插件名称并保留原始异常。"""
    event_manager = EventManager()
    plugin_manager = PluginManager()
    plugin = FailingDiscoverPlugin()
    errors: list[dict] = []
    monkeypatch.setattr(
        plugin_manager,
        "_plugins",
        {FailingDiscoverPlugin.__name__: FailingDiscoverPlugin},
    )
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {FailingDiscoverPlugin.__name__: plugin},
    )
    monkeypatch.setattr(
        event_manager,
        "_EventManager__handle_event_error",
        lambda **kwargs: errors.append(kwargs),
    )
    event = Event(ChainEventType.DiscoverSource)

    event_manager._EventManager__invoke_handler_by_type_sync(
        FailingDiscoverPlugin.handle, event
    )
    await event_manager._EventManager__invoke_handler_by_type_async(
        FailingDiscoverPlugin.handle,
        event,
    )

    assert [error["module_name"] for error in errors] == [
        "测试发现插件",
        "测试发现插件",
    ]
    assert all(str(error["e"]) == "discover failed" for error in errors)
