"""
Jellyfin 删除事件的路径恢复及整理历史定位回归。
"""

import json
from unittest.mock import Mock

import pytest

from app.db.models.transferhistory import TransferHistory
from app.db.oper.transferhistory import TransferHistoryOper
from app.modules.jellyfin.jellyfin import Jellyfin
from app.runtime.cache import MemoryBackend
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def client(monkeypatch):
    """
    隔离路径缓存并禁止媒体服务器真实请求。
    """
    monkeypatch.setattr(MemoryBackend, "_region_caches", {})
    server = Jellyfin.__new__(Jellyfin)
    server._host = "http://jellyfin.local/"
    monkeypatch.setattr(server, "get_item_path_by_id", Mock(return_value="/media/deleted.mkv"))
    monkeypatch.setattr(server, "get_remote_image_by_id", Mock(return_value="image"))
    return server


def _payload(event="ItemDeleted", **kwargs):
    """
    构造同一电影条目的 webhook 负载。
    """
    return json.dumps({
        "NotificationType": event, "ItemId": "deleted-copy", "ItemType": "Movie",
        "Name": "签约幽会", "Year": 2017, "Provider_tmdb": "468205", **kwargs,
    })


def test_deleted_item_uses_payload_path_without_remote_lookup(client):
    """
    删除负载已有路径时无需访问已消失条目的图片或播放接口。
    """
    event = client.get_webhook_message(_payload(Path="/media/payload.mkv"))

    assert event.item_path == "/media/payload.mkv"
    client.get_item_path_by_id.assert_not_called()
    client.get_remote_image_by_id.assert_not_called()


@pytest.mark.parametrize("event_type", ["ItemAdded", "PlaybackStart", "PlaybackStop"])
def test_deleted_item_recovers_observed_path_and_consumes_cache(client, event_type):
    """
    入库和播放查询到的路径可用于删除事件，消费后不再复用陈旧路径。
    """
    first = client.get_webhook_message(_payload(event_type))
    deleted = client.get_webhook_message(_payload())
    duplicate = client.get_webhook_message(_payload())

    assert first.item_path == deleted.item_path == "/media/deleted.mkv"
    assert duplicate.item_path is None
    client.get_item_path_by_id.assert_called_once_with("deleted-copy")
    client.get_remote_image_by_id.assert_called_once()


def test_payload_path_overrides_and_invalidates_cached_path(client):
    """
    删除负载路径优先于旧缓存，删除后清理缓存防止重用。
    """
    client.get_webhook_message(_payload("ItemAdded"))
    deleted = client.get_webhook_message(_payload(Path="/media/new.mkv"))

    assert deleted.item_path == "/media/new.mkv"
    assert client.get_webhook_message(_payload()).item_path is None


@pytest.mark.parametrize("path", [None, "", "  ", 42])
def test_unknown_deleted_path_is_reported_without_remote_lookup(client, monkeypatch, path):
    """
    未观察过的删除条目保留路径未知并告警，不能反查或猜测同片路径。
    """
    warning = Mock()
    monkeypatch.setattr("app.modules.jellyfin.jellyfin.logger.warning", warning)

    event = client.get_webhook_message(_payload(Path=path))

    assert event.item_path is None
    warning.assert_called_once()
    client.get_item_path_by_id.assert_not_called()


def test_webhook_path_cache_isolates_servers_and_items(client):
    """
    不同服务器或不同条目不得复用同一电影的缓存路径。
    """
    client.get_webhook_message(_payload("ItemAdded"))
    assert client.get_webhook_message(_payload(ItemId="other-copy")).item_path is None
    client._host = "http://other-jellyfin.local/"
    assert client.get_webhook_message(_payload()).item_path is None
    client._host = "http://jellyfin.local/"
    assert client.get_webhook_message(_payload()).item_path == "/media/deleted.mkv"


def test_non_deleted_payload_path_refreshes_cache_without_playback_lookup(client):
    """
    非删除负载自带路径时直接缓存，播放接口失败不影响已知路径。
    """
    client.get_webhook_message(_payload("ItemAdded", Path="/media/payload.mkv"))

    assert client.get_webhook_message(_payload()).item_path == "/media/payload.mkv"
    client.get_item_path_by_id.assert_not_called()


def test_failed_lookup_does_not_erase_observed_path(client):
    """
    一次播放查询失败不能擦除之前观察到的有效路径。
    """
    client.get_webhook_message(_payload("ItemAdded"))
    client.get_item_path_by_id.return_value = None
    client.get_webhook_message(_payload("PlaybackStart"))

    assert client.get_webhook_message(_payload()).item_path == "/media/deleted.mkv"


@pytest.mark.parametrize("observed", [True, False])
def test_deleted_copy_does_not_match_active_transfer_history(client, db, observed):
    """
    复现只有存活副本有整理记录的场景：已删副本或未知路径均匹配零条。
    """
    db.watermark(TransferHistory)
    db.add(TransferHistory(
        title="签约幽会", year="2017", type=MediaType.MOVIE.value,
        media_source=str(MediaSource.TMDB), media_id="468205", src="/downloads/source.mkv",
        dest="/media/active.mkv", src_storage="local", dest_storage="local",
    ))
    if observed:
        client.get_webhook_message(_payload("ItemAdded"))
    client.get_item_path_by_id.return_value = None
    event = client.get_webhook_message(_payload())

    assert TransferHistoryOper(db.session).get_by(
        mtype=MediaType.MOVIE.value, media_source=event.media_source,
        media_id=event.media_id, dest=event.item_path or "",
    ) == []
