"""
Transmission 未配置状态和 SDK 参数转发的兼容边界。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from transmission_rpc import Torrent

import app.modules.transmission.transmission as client_module
from app.modules.transmission.transmission import Transmission


@pytest.fixture
def state_clock(monkeypatch):
    """
    使用虚拟时钟验证启停等待上限，不让回归测试实际等待五秒。
    """
    clock = SimpleNamespace(now=0.0)

    def advance(seconds: float) -> None:
        """
        记录每次轮询等待的时间。
        """
        assert seconds > 0
        clock.now += seconds

    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=advance))
    return clock


@pytest.mark.parametrize("host", [None, "", "invalid-address"])
@pytest.mark.parametrize("rename_partial_files", [None, False, True])
def test_missing_address_keeps_state_and_reconnect_safe(monkeypatch, host, rename_partial_files):
    """
    未配置或地址解析失败时不连接网络，状态查询与重连均能正常结束。
    """
    factory = Mock()
    monkeypatch.setattr(client_module.transmission_rpc, "Client", factory)
    monkeypatch.setattr(client_module.UrlUtils, "parse_url_params", lambda **_kwargs: None)
    client = Transmission(host=host, rename_partial_files=rename_partial_files)
    assert client.trc is None
    assert client.is_inactive() is False
    assert client.reconnect() is None
    assert client.get_torrents() == ([], True)
    assert client.get_completed_torrents() is None
    assert client.get_downloading_torrents() is None
    assert client.change_torrent("hash", download_limit=0) is False
    factory.assert_not_called()


@pytest.mark.parametrize("rename_partial_files", [None, False, True])
def test_configured_partial_file_flag_preserves_raw_value(monkeypatch, rename_partial_files):
    """
    配置中的空值与布尔值保留原样，继续由登录层决定 SDK 开关。
    """
    sdk = Mock()
    monkeypatch.setattr(Transmission, "_Transmission__login_transmission", lambda _self: sdk)
    client = Transmission(host="transmission.invalid", port=9091, rename_partial_files=rename_partial_files)
    assert client.trc is sdk
    assert client._rename_partial_files is rename_partial_files
    assert client.is_inactive() is False


@pytest.mark.parametrize("protocol", [None, "http", "https"])
def test_explicit_protocol_is_forwarded_without_coercion(monkeypatch, protocol):
    """
    已有 protocol 扩展参数仍原样传给 SDK，包括显式空值。
    """
    sdk = Mock()
    sdk.get_session.return_value = {"rename-partial-files": True}
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(client_module.transmission_rpc, "Client", factory)
    client = Transmission(host="transmission.invalid", port=9091, protocol=protocol)
    assert client.trc is sdk
    assert factory.call_args.kwargs["protocol"] is protocol


@pytest.mark.parametrize("download_limit,upload_limit", [(None, None), (None, 1), (1, None)])
def test_incomplete_speed_limits_keep_failure_without_sdk_write(download_limit, upload_limit):
    """
    缺少任一限速时维持失败返回，不能误把缺省参数当作关闭限速。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    assert client.set_speed_limit(download_limit, upload_limit) is False
    client.trc.set_session.assert_not_called()


@pytest.mark.parametrize("download_limit,upload_limit", [(0, 0), (1.5, 20.9)])
def test_explicit_speed_limits_preserve_integer_conversion(download_limit, upload_limit):
    """
    显式零值仍关闭限速，非零值保留原来的整数转换和开关行为。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    assert client.set_speed_limit(download_limit, upload_limit) is True
    client.trc.set_session.assert_called_once_with(
        speed_limit_down=int(download_limit),
        speed_limit_up=int(upload_limit),
        speed_limit_down_enabled=bool(download_limit),
        speed_limit_up_enabled=bool(upload_limit),
    )


@pytest.mark.parametrize("method,field", [
    ("set_files", "files_wanted"), ("set_unwanted_files", "files_unwanted"),
])
@pytest.mark.parametrize("file_ids", [[], [0], [0, 2]])
def test_file_selection_never_sends_empty_rpc_arrays(method, field, file_ids) -> None:
    """
    空选择不得发送代表全部文件的 RPC 数组，非空索引应原样转发。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()

    assert getattr(client, method)("hash", file_ids) is True

    if file_ids:
        client.trc.change_torrent.assert_called_once_with(ids="hash", **{field: file_ids})
    else:
        client.trc.change_torrent.assert_not_called()


@pytest.mark.parametrize("method", ["set_files", "set_unwanted_files"])
@pytest.mark.parametrize("file_ids", [[], [0]])
def test_file_selection_without_connection_keeps_failure(method, file_ids) -> None:
    """
    未连接时仍返回失败，空选择不能掩盖客户端不可用状态。
    """
    client = Transmission.__new__(Transmission)
    client.trc = None

    assert getattr(client, method)("hash", file_ids) is False


@pytest.mark.parametrize("paused", [False, True])
def test_state_confirmation_without_connection_fails(paused) -> None:
    """
    未连接时不能把启停状态解释为成功。
    """
    client = Transmission.__new__(Transmission)
    client.trc = None

    assert client.ensure_torrent_state("hash", paused=paused) is False


@pytest.mark.parametrize("status", [1, 2, 3, 4, 5, 6])
def test_started_state_accepts_sdk_active_and_queued_statuses(state_clock, status) -> None:
    """
    启动请求后校验、下载及做种的活动或排队状态均可确认，不要求实际流量。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.return_value = Torrent(fields={"id": 1, "status": status})

    assert client.ensure_torrent_state("hash", paused=False) is True

    client.trc.start_torrent.assert_called_once_with(ids="hash")
    client.trc.stop_torrent.assert_not_called()
    assert state_clock.now == 0
    assert client.trc.get_torrent.call_args.kwargs["arguments"] == ["status"]


def test_pause_waits_for_async_stop_to_complete(state_clock) -> None:
    """
    停止请求已接受但仍在运行时继续等待，确认暂停前不能返回成功。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.side_effect = [
        SimpleNamespace(status=status) for status in ["downloading", "downloading", "stopped"]
    ]

    assert client.ensure_torrent_state("hash", paused=True) is True

    client.trc.stop_torrent.assert_called_once_with(ids="hash")
    client.trc.start_torrent.assert_not_called()
    assert state_clock.now > 0


def test_start_retries_stopped_task_until_queue_accepts_it(state_clock) -> None:
    """
    启动后仍停止时重发启动，进入下载队列后立即确认，保留正常排队语义。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.side_effect = [
        SimpleNamespace(status=status) for status in ["stopped", "download pending"]
    ]

    assert client.ensure_torrent_state("hash", paused=False) is True

    assert client.trc.start_torrent.call_count == 2
    assert state_clock.now > 0


def test_start_retry_rpc_failure_never_confirms_success(state_clock) -> None:
    """
    首次启动已接受但重试断连时，仍须返回失败而非沿用首次 RPC 的成功。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.return_value = SimpleNamespace(status="stopped")
    client.trc.start_torrent.side_effect = [None, ConnectionError("重试启动失败")]

    assert client.ensure_torrent_state("hash", paused=False) is False
    assert state_clock.now == 0


@pytest.mark.parametrize("paused,status", [(True, "downloading"), (False, "stopped")])
def test_state_confirmation_timeout_is_bounded(state_clock, paused, status) -> None:
    """
    RPC 接受命令但状态始终未改变时，在确认窗口内结束并报告失败。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.return_value = SimpleNamespace(status=status)

    assert client.ensure_torrent_state("hash", paused=paused) is False

    assert state_clock.now == client_module._TORRENT_STATE_TIMEOUT
    assert all(0 < call.kwargs["timeout"] <= client_module._TORRENT_STATE_TIMEOUT
               for call in client.trc.get_torrent.call_args_list)
    if paused:
        client.trc.stop_torrent.assert_called_once_with(ids="hash")
    else:
        assert client.trc.start_torrent.call_count > 1


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("error", [KeyError("任务不存在"), ConnectionError("RPC 查询失败")])
def test_state_query_failure_never_confirms_success(state_clock, paused, error) -> None:
    """
    查询失败或任务已消失时返回失败，不能把 RPC 接受命令当成状态确认。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.side_effect = error

    assert client.ensure_torrent_state("hash", paused=paused) is False
    assert state_clock.now == 0


@pytest.mark.parametrize("paused", [False, True])
def test_state_change_rpc_failure_never_confirms_success(state_clock, paused) -> None:
    """
    启停命令失败时立即返回，暂停失败不应继续查询或重发启动。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.trc.get_torrent.return_value = SimpleNamespace(status="downloading")
    command = client.trc.stop_torrent if paused else client.trc.start_torrent
    command.side_effect = ConnectionError("RPC 启停失败")

    assert client.ensure_torrent_state("hash", paused=paused) is False
    assert state_clock.now == 0
