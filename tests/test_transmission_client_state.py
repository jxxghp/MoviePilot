"""
Transmission 未配置状态和 SDK 参数转发的兼容边界。
"""

from unittest.mock import Mock

import pytest

import app.modules.transmission.transmission as client_module
from app.modules.transmission.transmission import Transmission


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
