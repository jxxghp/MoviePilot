"""服务实例健康探针插件的守护测试。

本插件是智能体工具族目前唯一的随仓参考实现，因此测试盯住两类事：

- **它是不是一个合规的智能体工具**：声明过得了登记期契约校验，工具名与描述在声明和
  实现两侧是同一份，标签里带着只读子代理据以筛选的 ``ToolTag.Read``。
- **它探出来的状态对不对**：停用的不探、没装载的不猜、探针抛异常不把异常泄进返回值，
  以及只调必填集里只读的那个方法——``reconnect`` 会重建连接，读状态的工具不碰它。
"""

import asyncio
from types import SimpleNamespace
from typing import Iterator

import pytest

from app.agent.tools.base import MoviePilotTool, shutdown_blocking_executors
from app.plugins.servicehealth import ServiceHealth
from app.plugins.servicehealth.probe import (
    FAMILY_HELPERS,
    FAMILY_QUERY_FAILED,
    STATE_ABSENT,
    STATE_DISABLED,
    STATE_NO_PROBE,
    STATE_PROBE_FAILED,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    ServiceInstanceHealthTool,
)
from app.runtime.extensions.admission import agent_tool
from app.sdk.agent import ToolTag
from app.sdk.service_instances import service_capabilities


class _FakeHelper:
    """按预置数据回答实例发现的服务帮助类替身。"""

    def __init__(self, configs: dict, services: dict):
        self._configs = configs
        self._services = services

    def get_configs(self, include_disabled: bool = False) -> dict:
        """返回预置的实例配置。"""
        return dict(self._configs)

    def get_services(self) -> dict:
        """返回预置的已装载实例。"""
        return dict(self._services)


def _config(name: str, service_type: str = "qbittorrent", enabled: bool = True):
    """构造一条实例配置替身。"""
    return SimpleNamespace(name=name, type=service_type, enabled=enabled)


def _downloader(inactive: bool = False):
    """构造一个下载器实例替身。"""
    return SimpleNamespace(is_inactive=lambda: inactive, reconnect=lambda: None)


def _tool() -> ServiceInstanceHealthTool:
    """构造工具实例，会话与用户标识取固定值。"""
    return ServiceInstanceHealthTool(session_id="probe-session", user_id="probe-user")


@pytest.fixture(autouse=True)
def _release_blocking_executors() -> Iterator[None]:
    """归还工具阻塞线程池，避免用例遗留 worker。"""
    yield
    shutdown_blocking_executors(cancel_futures=True)


@pytest.fixture(autouse=True)
def _inject_agent_tool_base() -> Iterator[None]:
    """快照并复原智能体工具基类注入状态。"""
    original = agent_tool._agent_tool_base
    agent_tool.configure_agent_tool_base(MoviePilotTool)
    try:
        yield
    finally:
        agent_tool._agent_tool_base = original


def test_declaration_passes_the_registration_contract():
    """插件交出的工具声明必须过得了登记期契约校验。"""
    declarations = ServiceHealth().provides_agent_tools()

    assert len(declarations) == 1
    assert agent_tool.agent_tool_declaration_violation(declarations[0]) is None


def test_declaration_and_implementation_report_the_same_identity():
    """声明数据与实现类字段必须是同一份工具名与描述。

    宿主取工具名时先看声明、取不到才回落到实现类字段，两处各写一份就会在某一条路径上
    答出另一个名字。
    """
    declaration = ServiceHealth().provides_agent_tools()[0]

    assert declaration.name == TOOL_NAME
    assert declaration.description == TOOL_DESCRIPTION
    assert agent_tool.agent_tool_declaration_name(declaration) == TOOL_NAME
    assert ServiceInstanceHealthTool.model_fields["name"].default == TOOL_NAME
    assert ServiceInstanceHealthTool.model_fields["description"].default == TOOL_DESCRIPTION


def test_the_tool_is_selectable_by_read_only_subagents():
    """工具必须带只读标签，否则只读子代理一次都选不到它。"""
    tags = {str(tag) for tag in _tool().tags}

    assert ToolTag.Read.value in tags
    assert ToolTag.AgentTool.value in tags


def test_the_covered_families_are_registered_service_families():
    """工具覆盖的族必须都是宿主登记过的服务族。"""
    assert set(FAMILY_HELPERS) <= set(service_capabilities())


def test_probe_method_only_picks_the_read_only_one():
    """探针只取必填集与只读方法表的交集，且交集恰好一个才用。"""
    assert ServiceInstanceHealthTool._probe_method("downloader") == "is_inactive"
    assert ServiceInstanceHealthTool._probe_method("mediaserver") == "is_inactive"
    assert ServiceInstanceHealthTool._probe_method("notification") == "get_state"
    assert ServiceInstanceHealthTool._probe_method("storage") is None


def test_the_tool_reports_live_state_per_instance(monkeypatch):
    """在线、需要重连、未启用与未装载四种情形各自报出各自的状态。"""
    configs = {
        "在线的": _config("在线的"),
        "掉线的": _config("掉线的"),
        "停用的": _config("停用的", enabled=False),
        "没装载的": _config("没装载的"),
    }
    services = {
        "在线的": SimpleNamespace(instance=_downloader(inactive=False)),
        "掉线的": SimpleNamespace(instance=_downloader(inactive=True)),
    }
    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS",
        {"downloader": lambda: _FakeHelper(configs, services)},
    )

    result = asyncio.run(_tool().run(capability="downloader"))

    assert "共 4 个实例" in result
    assert "- 在线的（qbittorrent）：在线" in result
    assert "- 掉线的（qbittorrent）：需要重连" in result
    assert f"- 停用的（qbittorrent）：{STATE_DISABLED}" in result
    assert f"- 没装载的（qbittorrent）：{STATE_ABSENT}" in result


def test_the_tool_never_calls_reconnect(monkeypatch):
    """探针不得调用会重建连接的必填方法。"""
    reconnected: list[str] = []
    instance = SimpleNamespace(
        is_inactive=lambda: True,
        reconnect=lambda: reconnected.append("called"),
    )
    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS",
        {
            "downloader": lambda: _FakeHelper(
                {"掉线的": _config("掉线的")},
                {"掉线的": SimpleNamespace(instance=instance)},
            )
        },
    )

    asyncio.run(_tool().run(capability="downloader"))

    assert reconnected == []


def test_probe_failure_does_not_leak_into_the_result(monkeypatch):
    """探针抛异常时只报固定文案，异常内容进日志不进返回值。"""

    def _explode():
        raise RuntimeError("host=10.0.0.1 token=secret")

    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS",
        {
            "downloader": lambda: _FakeHelper(
                {"炸了的": _config("炸了的")},
                {
                    "炸了的": SimpleNamespace(
                        instance=SimpleNamespace(
                            is_inactive=_explode, reconnect=lambda: None
                        )
                    )
                },
            )
        },
    )

    result = asyncio.run(_tool().run(capability="downloader"))

    assert STATE_PROBE_FAILED in result
    assert "secret" not in result


def test_family_query_failure_does_not_leak_into_the_result(monkeypatch):
    """整族取不出实例时同样只报固定文案。"""

    def _explode():
        raise RuntimeError("host=10.0.0.1 token=secret")

    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS", {"downloader": _explode}
    )

    result = asyncio.run(_tool().run(capability="downloader"))

    assert FAMILY_QUERY_FAILED in result
    assert "secret" not in result


def test_family_without_a_read_only_probe_reports_no_probe(monkeypatch):
    """必填集里没有只读方法的族只报配置不报状态。"""
    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS",
        {
            "storage": lambda: _FakeHelper(
                {"网盘": _config("网盘", service_type="p123")},
                {"网盘": SimpleNamespace(instance=object())},
            )
        },
    )

    result = asyncio.run(_tool().run(capability="storage"))

    assert STATE_NO_PROBE in result


def test_unregistered_capability_is_rejected_with_the_candidate_list():
    """未登记的族当场拒绝，并报出当前可查的族。"""
    result = asyncio.run(_tool().run(capability="nonexistent-family"))

    assert "未登记" in result
    assert "downloader" in result


def test_registered_but_unprobeable_capability_says_so():
    """登记了却没有只读状态方法的族，回答「查不了」而不是「未登记」。"""
    result = asyncio.run(_tool().run(capability="auth"))

    assert "未登记" not in result
    assert "探不了" in result


def test_empty_capability_covers_every_supported_family(monkeypatch):
    """不给参数时逐族查询，各族都出现在结果里。"""
    monkeypatch.setattr(
        "app.plugins.servicehealth.probe.FAMILY_HELPERS",
        {
            "downloader": lambda: _FakeHelper({}, {}),
            "notification": lambda: _FakeHelper({}, {}),
        },
    )

    result = asyncio.run(_tool().run())

    assert "【下载器】未配置任何实例" in result
    assert "【消息通知】未配置任何实例" in result
