"""
Emby / Jellyfin 媒体库同步读取失败时保护旧缓存的回归。
"""

from unittest.mock import Mock, patch

import pytest

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin

_REQUEST_TARGETS = {
    Emby: "app.modules.emby.emby.RequestUtils",
    Jellyfin: "app.modules.jellyfin.jellyfin.RequestUtils",
}


def _client(client_class: type):
    """
    构造跳过远端初始化的媒体服务器客户端。
    """
    instance = client_class.__new__(client_class)
    instance._host = "http://mediaserver.local/"
    instance._apikey = "api-key"
    instance._playhost = None
    instance._sync_libraries = []
    instance.user = "user-id"
    return instance


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("response", [None, Mock(status_code=500)])
def test_get_items_raises_when_library_read_fails(client_class: type, response):
    """
    列表请求失败必须中止同步，不能产出空结果让同步链删除该服务器的旧缓存。
    """
    client = _client(client_class)
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.return_value = response
        with pytest.raises(ConnectionError):
            list(client.get_items("movies"))


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_get_items_raises_when_nested_folder_read_fails(client_class: type):
    """
    子文件夹读取失败同样中止同步，避免只同步到部分条目。
    """
    client = _client(client_class)
    folder = Mock(status_code=200)
    folder.json.return_value = {"Items": [{"Id": "folder-1", "Type": "Folder"}]}
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = [folder, TimeoutError("read timeout")]
        with pytest.raises(TimeoutError, match="read timeout"):
            list(client.get_items("movies"))


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_get_items_raises_when_server_is_not_connected(client_class: type):
    """
    未连接的服务器不得被同步为空媒体库。
    """
    client = _client(client_class)
    client._apikey = None

    with pytest.raises(ConnectionError):
        list(client.get_items("movies"))
