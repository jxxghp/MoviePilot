"""
虚拟显示托管资源与旧 API 的兼容测试。
"""

from __future__ import annotations

import os
import sys
from types import ModuleType
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from app.adapters.system.display import DisplayHelper
from app.adapters.system.display.resource import VirtualDisplayResource
from app.foundation.singleton import Singleton


def test_virtual_display_skips_host_process_outside_docker(monkeypatch) -> None:
    """
    非容器环境启动资源时不得创建虚拟显示进程。

    :param monkeypatch: 隔离容器环境判定的测试夹具
    """
    monkeypatch.setattr(
        "app.adapters.system.display.resource.SystemUtils.is_docker",
        lambda: False,
    )
    resource = VirtualDisplayResource()

    resource.start()
    resource.stop()

    assert resource.display is None


def test_virtual_display_starts_and_stops_owned_process(monkeypatch) -> None:
    """
    容器环境只停止当前资源实际拥有的显示进程。

    :param monkeypatch: 隔离显示后端与容器环境的测试夹具
    """
    events: list[object] = []

    class FakeDisplay:
        """
        记录 pyvirtualdisplay 的构造与生命周期。
        """

        def __init__(self, **kwargs) -> None:
            """
            记录资源交给显示后端的启动参数。

            :param kwargs: 显示后端的构造选项
            """
            events.append(("create", kwargs))

        def start(self) -> None:
            """
            记录虚拟显示启动调用，不创建实际显示进程。
            """
            events.append("start")

        def stop(self) -> None:
            """
            记录虚拟显示停止调用，供资源所有权断言使用。
            """
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
            },
        ),
        "start",
        "stop",
    ]
    assert resource.display is None


@pytest.mark.parametrize("original_display", [":987", None])
def test_virtual_display_allocates_number_and_restores_environment(
    monkeypatch: pytest.MonkeyPatch,
    original_display: Optional[str],
) -> None:
    """
    真实显示后端自动分配编号，重复启停后恢复原有或缺省的 DISPLAY。

    只替换 Xvfb 进程边界，保留命令构造、displayfd 管道和环境变量管理。

    :param monkeypatch: 隔离进程启动、容器判定和环境变量的测试夹具
    :param original_display: 启动前的显示地址；None 表示未配置 DISPLAY
    """
    commands: list[list[str]] = []
    processes: list[MagicMock] = []
    display_numbers = iter((1001, 1002))

    def spawn_display(
        command: list[str], *, pass_fds: list[int], **_kwargs: Any,
    ) -> MagicMock:
        """
        模拟 Xvfb 通过 displayfd 回传分配编号并保留可停止的进程句柄。

        :param command: 真实显示后端拼接的启动命令
        :param pass_fds: 交给 Xvfb 回传显示编号的管道描述符
        :param _kwargs: 子进程启动时保留的输出及 shell 选项
        :return: 供显示后端查询和停止的进程替身
        """
        commands.append(command)
        process = MagicMock()
        process.poll.return_value = None
        process.communicate.return_value = (b"", b"")
        processes.append(process)
        os.write(pass_fds[0], f"{next(display_numbers)}\n".encode("ascii"))
        return process

    monkeypatch.setattr(
        "app.adapters.system.display.resource.SystemUtils.is_docker", lambda: True,
    )
    monkeypatch.setattr(
        "pyvirtualdisplay.abstractdisplay.get_helptext", lambda _program: "-displayfd",
    )
    monkeypatch.setattr("pyvirtualdisplay.abstractdisplay.subprocess.Popen", spawn_display)
    monkeypatch.setenv("PYVIRTUALDISPLAY_DISPLAYFD", "1")
    if original_display is None:
        monkeypatch.delenv("DISPLAY", raising=False)
    else:
        monkeypatch.setenv("DISPLAY", original_display)
    resource = VirtualDisplayResource()

    try:
        for display_number in (1001, 1002):
            resource.start()
            display = resource.display
            resource.start()

            assert resource.display is display
            assert os.environ["DISPLAY"] == f":{display_number}"
            assert "-displayfd" in commands[-1]
            assert not any(argument.startswith(":") for argument in commands[-1])

            resource.stop()
            resource.stop()
            assert resource.display is None
            assert os.environ.get("DISPLAY") == original_display
    finally:
        resource.stop()

    assert len(commands) == 2
    for process in processes:
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with()


def test_display_helper_keeps_legacy_constructor_and_stop_contract(monkeypatch) -> None:
    """
    旧构造入口显式激活 host.display，stop 只停止已配置 Runtime。

    :param monkeypatch: 隔离 Runtime 资源调用的测试夹具
    """
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
