"""
Chain 外部服务与系统技术端口的装配和隔离测试。
"""

from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain import _recognition as recognition
from app.chain.subscribe import notify as subscription
from app.chain.transfer import filter as transfer
from app.startup.composition import chain as chain_composition
from app.startup.composition import subscription as subscription_composition
from app.startup.initializers import chain as chain_initializer


def _assert_all_ports_unconfigured() -> None:
    """
    断言三个 Chain 技术端口都处于明确的未装配状态。
    """
    with pytest.raises(RuntimeError, match="共享识别端口尚未"):
        # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
        recognition._recognition_share_snapshot()  # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="订阅共享端口尚未"):
        # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
        subscription._subscription_share_snapshot()  # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="整理文件系统端口尚未"):
        # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
        transfer._network_filesystem_snapshot()  # pylint: disable=protected-access


def test_unconfigured_chain_ports_fail_stably() -> None:
    """
    绕过 startup 的调用应稳定失败，不能隐式导入具体 Adapter。
    """
    chain_initializer.reset_chain_ports()

    _assert_all_ports_unconfigured()


def test_configure_returns_previous_port_for_scoped_restore() -> None:
    """
    重复装配应返回旧实例，并允许隔离环境精确恢复。
    """
    first = Mock()
    second = Mock()
    chain_initializer.reset_chain_ports()

    assert recognition.configure_recognition_share_port(first) is None
    assert recognition.configure_recognition_share_port(second) is first
    recognition.reset_recognition_share_port(first)
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    assert recognition._recognition_share_snapshot() is first  # pylint: disable=protected-access

    assert subscription.configure_subscription_share_port(first) is None
    assert subscription.configure_subscription_share_port(second) is first
    subscription.reset_subscription_share_port(first)
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    assert subscription._subscription_share_snapshot() is first  # pylint: disable=protected-access

    assert transfer.configure_network_filesystem_port(first) is None
    assert transfer.configure_network_filesystem_port(second) is first
    transfer.reset_network_filesystem_port(first)
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    assert transfer._network_filesystem_snapshot() is first  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_chain_adapters_preserve_sync_and_async_behavior(monkeypatch) -> None:
    """
    startup 适配器应逐项转发参数和同步、异步返回值。
    """
    report_recognition = Mock(return_value=True)
    async_report_recognition = AsyncMock(return_value=False)
    query_recognition = Mock(return_value={"media_id": "1"})
    async_query_recognition = AsyncMock(return_value={"media_id": "2"})
    normalize_recognition = Mock(return_value={"media_source": "tmdb"})
    report_added = Mock(return_value=True)
    async_report_added = AsyncMock(return_value=False)
    list_shares = Mock(return_value=[{"share_uid": "u1"}])
    report_completed = Mock(return_value=True)
    filesystem = Mock(return_value=True)
    helper = chain_composition.MoviePilotServerHelper
    monkeypatch.setattr(helper, "report_recognize_share", report_recognition)
    monkeypatch.setattr(helper, "async_report_recognize_share", async_report_recognition)
    monkeypatch.setattr(helper, "query_recognize_share", query_recognition)
    monkeypatch.setattr(helper, "async_query_recognize_share", async_query_recognition)
    monkeypatch.setattr(helper, "to_recognize_params", normalize_recognition)
    monkeypatch.setattr(helper, "sub_reg_durable", report_added)
    monkeypatch.setattr(helper, "async_sub_reg_durable", async_report_added)
    monkeypatch.setattr(helper, "get_subscribe_shares", list_shares)
    monkeypatch.setattr(helper, "sub_done_durable", report_completed)
    monkeypatch.setattr(
        chain_composition.SystemUtils,
        "is_network_filesystem",
        filesystem,
    )
    chain_initializer.init_chain_ports()

    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    recognition_port = recognition._recognition_share_snapshot()  # pylint: disable=protected-access
    assert recognition_port.report_recognize_share(None, None) is True
    assert await recognition_port.async_report_recognize_share(None, None) is False
    assert recognition_port.query_recognize_share(None) == {"media_id": "1"}
    assert await recognition_port.async_query_recognize_share(None) == {"media_id": "2"}
    assert recognition_port.to_recognize_params({"id": "1"}) == {"media_source": "tmdb"}

    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    subscription_port = subscription._subscription_share_snapshot()  # pylint: disable=protected-access
    assert subscription_port.report_added({"id": 1}) is True
    assert await subscription_port.async_report_added({"id": 2}) is False
    assert subscription_port.list_shares() == [{"share_uid": "u1"}]
    assert subscription_port.report_completed({"id": 3}) is True

    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    filesystem_port = transfer._network_filesystem_snapshot()  # pylint: disable=protected-access
    assert filesystem_port.is_network_filesystem(
        Path("/media"), include_local_fuse=True
    ) is True
    filesystem.assert_called_once_with(Path("/media"), include_local_fuse=True)


@pytest.mark.asyncio
async def test_subscription_delete_reporting_ports_copy_readonly_payload(monkeypatch) -> None:
    """
    删除统计端口应把只读映射转换为普通字典后再交给服务端适配器。
    """
    async_report = AsyncMock(return_value=True)
    sync_report = Mock(return_value=True)
    helper = subscription_composition.MoviePilotServerHelper
    monkeypatch.setattr(helper, "async_sub_done_durable", async_report)
    monkeypatch.setattr(helper, "sub_done_durable", sync_report)
    payload = MappingProxyType({"subscribe_id": 7})

    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    assert await subscription_composition._report_deleted(payload) is True  # pylint: disable=protected-access
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    assert subscription_composition._report_deleted_sync(payload) is True  # pylint: disable=protected-access
    async_report.assert_awaited_once_with({"subscribe_id": 7})
    sync_report.assert_called_once_with({"subscribe_id": 7})


def test_chain_port_initializer_rolls_back_partial_failure(monkeypatch) -> None:
    """
    任一端口装配失败时应清理已装配实例并保留原异常。
    """
    failure = RuntimeError("subscription adapter failed")
    monkeypatch.setattr(
        chain_composition,
        "configure_subscription_share_port",
        Mock(side_effect=failure),
    )

    with pytest.raises(RuntimeError, match="subscription adapter failed"):
        chain_initializer.init_chain_ports()

    _assert_all_ports_unconfigured()


def test_chain_port_init_and_reset_are_repeatable() -> None:
    """
    重复 lifespan 装配和释放不应残留上一轮端口。
    """
    chain_initializer.init_chain_ports()
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    first_recognition = recognition._recognition_share_snapshot()  # pylint: disable=protected-access
    chain_initializer.init_chain_ports()
    # 直接读取内部端口快照或适配函数，验证注入与重置的边界契约。
    second_recognition = recognition._recognition_share_snapshot()  # pylint: disable=protected-access

    assert second_recognition is not first_recognition
    chain_initializer.reset_chain_ports()
    chain_initializer.reset_chain_ports()
    _assert_all_ports_unconfigured()
