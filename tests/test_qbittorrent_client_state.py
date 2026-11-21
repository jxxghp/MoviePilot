"""
qBittorrent 未配置状态和强制作种开关兼容性。
"""

from unittest.mock import Mock

import pytest

import app.modules.qbittorrent.qbittorrent as client_module
from app.modules.qbittorrent.qbittorrent import Qbittorrent


@pytest.mark.parametrize("host", [None, ""])
@pytest.mark.parametrize("force_resume", [None, False, True])
def test_missing_host_keeps_state_and_reconnect_safe(monkeypatch, host, force_resume):
    """
    缺少地址时状态查询及重连不应崩溃或尝试网络连接。
    """
    factory = Mock()
    monkeypatch.setattr(client_module.qbittorrentapi, "Client", factory)
    client = Qbittorrent(host=host, force_resume=force_resume)
    assert client.qbc is None
    assert client.is_inactive() is False
    assert client.is_force_resume() is force_resume
    assert client.reconnect() is None
    assert client.qbc is None
    assert client.get_torrents() == ([], True)
    assert client.get_completed_torrents() is None
    assert client.get_downloading_torrents() is None
    factory.assert_not_called()


@pytest.mark.parametrize("force_resume", [None, False, True])
def test_configured_force_resume_preserves_raw_flag(monkeypatch, force_resume):
    """
    保留显式 None、False 和 True，不在类型收紧时隐式改变开关值。
    """
    sdk = Mock()
    monkeypatch.setattr(Qbittorrent, "_Qbittorrent__login_qbittorrent", lambda _self: sdk)
    client = Qbittorrent(host="https://qb.invalid", port=443, force_resume=force_resume)
    assert client.qbc is sdk
    assert client.is_inactive() is False
    assert client.is_force_resume() is force_resume


def test_tag_filter_ignores_spaces_after_commas():
    """
    TORRENT_TAG 配成 "MOVIEPILOT, TV" 时，整理轮询按去掉空白后的标签匹配，与已有任务受理确认标签的解析一致；
    否则受理时确认两个标签都在，轮询却在找带前导空格的 " TV"，追加的剧集下完也不会整理。
    """
    client = Qbittorrent.__new__(Qbittorrent)
    client.qbc = Mock()
    client.qbc.torrents_info.return_value = [
        {"hash": "both", "tags": "MOVIEPILOT, TV"}, {"hash": "one", "tags": "MOVIEPILOT"},
    ]

    torrents, error = client.get_torrents(tags="MOVIEPILOT, TV")

    assert error is False
    assert [torrent["hash"] for torrent in torrents] == ["both"]
