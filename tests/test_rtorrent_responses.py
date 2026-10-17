"""
验证 rTorrent XML-RPC 返回值映射及异常响应的隔离。
"""

from unittest.mock import Mock

import pytest

from app.modules.rtorrent.rtorrent import Rtorrent


@pytest.fixture
def client():
    """
    构造无网络客户端，所有远端调用均由 Mock 接管。
    """
    result = Rtorrent.__new__(Rtorrent)
    result._proxy = Mock()
    return result


@pytest.fixture
def torrent_row():
    """
    提供与 d.multicall2 字段顺序一致的种子记录。
    """
    return ["ABC", "movie", 1000, 250, 10, 20, 1, 0,
            "/downloads", "movie, keep", 1, 1, "1500", "/downloads/movie"]


def test_torrent_mapping_keeps_progress_ratio_and_filters(client, torrent_row):
    """
    有效响应保留字段原值、百分比和千分比，过滤不改变记录。
    """
    client._proxy.d.multicall2.return_value = [torrent_row]
    torrents, error = client.get_torrents(ids="abc", tags=["keep"])
    assert error is False
    assert torrents[0]["progress"] == 25.0
    assert torrents[0]["ratio"] == 1.5
    assert torrents[0]["total_size"] == 1000
    assert torrents[0]["content_path"] == "/downloads/movie"
    assert client.get_torrents(ids=["other"]) == ([], False)
    assert client.get_torrents(tags="other") == ([], False)
    assert client.get_downloading_torrents() == torrents
    assert client.get_completed_torrents() == []


def test_zero_size_and_tuple_rows_remain_supported(client, torrent_row):
    """
    XML-RPC 数组支持列表和元组，零大小任务保持零进度。
    """
    torrent_row[2] = 0
    torrent_row[12] = 0
    client._proxy.d.multicall2.return_value = (tuple(torrent_row),)
    torrents, error = client.get_torrents()
    assert error is False
    assert torrents[0]["progress"] == torrents[0]["ratio"] == 0


@pytest.mark.parametrize("payload", [None, 123, "wrong", [123], [["short"]]])
def test_malformed_multicall_keeps_error_contract(client, payload):
    """
    错误的远端数组仍返回原有失败标记，不泄漏部分结果。
    """
    client._proxy.d.multicall2.return_value = payload
    assert client.get_torrents() == ([], True)


@pytest.mark.parametrize("index,value", [(0, 12), (2, "1000"), (9, ["tag"]), (12, {"invalid": True})])
def test_invalid_torrent_fields_keep_error_contract(client, torrent_row, index, value):
    """
    身份、算术和标签字段不接受不符合 XML-RPC 合同的类型。
    """
    torrent_row[index] = value
    client._proxy.d.multicall2.return_value = [torrent_row]
    assert client.get_torrents() == ([], True)


def test_file_chunk_progress_accepts_numeric_strings(client):
    """
    文件分块计数继续兼容字符串，并避免零分母。
    """
    client._proxy.f.multicall.return_value = [
        ["one", 100, 1, "2", "4"], ["two", 0, 0, 0, 0],
    ]
    assert client.get_files("ABC") == [
        {"id": 0, "name": "one", "size": 100, "priority": 1, "progress": 50.0},
        {"id": 1, "name": "two", "size": 0, "priority": 0, "progress": 0},
    ]
    client._proxy.f.multicall.return_value = [["bad", 100, 1, {}, 4]]
    assert client.get_files("ABC") is None


def test_tag_operations_preserve_merge_remove_and_empty_response(client):
    """
    标签的合并、删除和空返回兼容原行为。
    """
    client._proxy.d.custom1.return_value = " old, keep "
    assert client.set_torrents_tag("ABC", ["new"])
    assert set(client._proxy.d.custom1.set.call_args.args[1].split(",")) == {"old", "keep", "new"}
    assert client.remove_torrents_tag("ABC", "old")
    client._proxy.d.custom1.set.assert_called_with("ABC", "keep")
    assert client.get_torrent_tags("ABC") == ["old", "keep"]
    client._proxy.d.custom1.return_value = None
    assert client.get_torrent_tags("ABC") == []
    client._proxy.d.custom1.return_value = ["invalid"]
    assert client.set_torrents_tag("ABC", ["new"]) is False
    assert client.get_torrent_tags("ABC") == []
