"""虚拟显示托管资源与旧 API 的兼容测试。"""

from __future__ import annotations

import sys
from types import ModuleType

from app.adapters.system.display import DisplayHelper
from app.adapters.system.display.resource import VirtualDisplayResource
from app.foundation.singleton import Singleton


def test_virtual_display_skips_host_process_outside_docker(monkeypatch) -> None:
    """非容器环境启动资源时不得创建虚拟显示进程。"""
    monkeypatch.setattr(
        "app.adapters.system.display.resource.SystemUtils.is_docker",
        lambda: False,
    )
    resource = VirtualDisplayResource()

    resource.start()
    resource.stop()

    assert resource.display is None


def test_virtual_display_starts_and_stops_owned_process(monkeypatch) -> None:
    """容器环境只停止当前资源实际拥有的显示进程。"""
    events: list[object] = []

    class FakeDisplay:
        """记录 pyvirtualdisplay 的构造与生命周期。"""

        def __init__(self, **kwargs) -> None:
            events.append(("create", kwargs))

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    pyvirtualdisplay = ModuleType("pyvirtualdisplay")
    pyvirtualdisplay.Display = FakeDisplay
    monkeypatch.setitem(sys.modules, "pyvirtualdisplay", pyvirtualdisplay)
    monkeypatch.setattr(
        "app.adapters.system.display.resource.SystemUtils.is_docker",
        lambda: True,
    )
    monkeypatch.setenv("DISPLAY", ":99")
    resource = VirtualDisplayResource()

    resource.start()
    resource.stop()
    resource.stop()

    assert events == [
        (
            "create",
            {
                "visible": False,
                "size": (1024, 768),
                "extra_args": [":99"],
            },
        ),
        "start",
        "stop",
    ]
    assert resource.display is None


def test_display_helper_keeps_legacy_constructor_and_stop_contract(monkeypatch) -> None:
    """旧构造入口显式激活 host.display，stop 只停止已配置 Runtime。"""
    events: list[tuple[str, str]] = []
    singleton_key = (DisplayHelper, (), frozenset())
    previous = Singleton._instances.pop(singleton_key, None)
    monkeypatch.setattr(
        "app.adapters.system.display.acquire_managed_resource",
        lambda capability_id, *, reason, retry: events.append(
            ("activate", capability_id)
        ),
    )
    monkeypatch.setattr(
        "app.adapters.system.display.stop_managed_resource",
        lambda capability_id, *, reason: events.append(("stop", capability_id)),
    )
    try:
        helper = DisplayHelper()
        assert DisplayHelper() is helper
        helper.stop()
    finally:
        Singleton._instances.pop(singleton_key, None)
        if previous is not None:
            Singleton._instances[singleton_key] = previous

    assert events == [
        ("activate", "host.display"),
        ("stop", "host.display"),
    ]
