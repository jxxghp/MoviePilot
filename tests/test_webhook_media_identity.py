import json
from unittest.mock import Mock

import pytest

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.zspace.zspace import ZSpace
from app.schemas.mediaserver import WebhookEventInfo
from app.schemas.types import MediaSource


def test_webhook_event_migrates_legacy_tmdb_input_without_serializing_legacy_field() -> None:
    """旧 tmdb_id 输入应迁移为统一身份，序列化时不再输出旧字段。"""
    event = WebhookEventInfo(tmdb_id="12345")

    assert event.media_source == MediaSource.TMDB
    assert event.media_id == "12345"
    assert event.tmdb_id == "12345"
    assert "tmdb_id" not in event.model_dump()


def test_webhook_event_legacy_tmdb_property_keeps_old_plugins_working() -> None:
    """旧插件通过 tmdb_id 属性读写时也应同步到统一身份。"""
    event = WebhookEventInfo()

    event.tmdb_id = 67890

    assert event.media_source == MediaSource.TMDB
    assert event.media_id == "67890"
    assert event.tmdb_id == "67890"


@pytest.mark.parametrize(
    "payload",
    [
        {"media_source": MediaSource.TMDB},
        {"media_id": "12345"},
        {"media_source": MediaSource.TMDB, "media_id": "0"},
    ],
)
def test_webhook_event_rejects_invalid_unified_identity(payload: dict) -> None:
    """Webhook 事件不得携带半对身份或零值 ID。"""
    with pytest.raises(ValueError):
        WebhookEventInfo(**payload)


def test_jellyfin_webhook_uses_provider_identity_pair() -> None:
    """Jellyfin webhook 应把 Provider 字段转换为统一身份。"""
    client = Jellyfin.__new__(Jellyfin)
    event = client.get_webhook_message(json.dumps({
        "NotificationType": "ItemAdded",
        "ItemType": "Movie",
        "Name": "测试电影",
        "Year": 2026,
        "Provider_tmdb": "1001",
    }))

    assert event is not None
    assert event.media_source == MediaSource.TMDB
    assert event.media_id == "1001"


@pytest.mark.parametrize("client_class", [Emby, ZSpace])
def test_emby_family_webhook_uses_provider_identity_pair(client_class: type) -> None:
    """Emby 系 webhook 应支持非 TMDB Provider 并输出统一身份。"""
    client = client_class.__new__(client_class)
    event = client.get_webhook_message({
        "data": json.dumps({
            "Event": "library.new",
            "Item": {
                "Type": "Movie",
                "Name": "测试电影",
                "ProductionYear": 2026,
                "ProviderIds": {"Douban": "2002"},
            },
        })
    }, {})

    assert event is not None
    assert event.media_source == MediaSource.Douban
    assert event.media_id == "2002"


@pytest.mark.parametrize("item_type", ["Episode", "Season"])
def test_emby_tv_webhook_uses_series_provider_identity(monkeypatch, item_type: str) -> None:
    """Episode 和 Season webhook 应使用 Series 条目的媒体主身份。"""
    client = Emby.__new__(Emby)
    get_series_identity = Mock(return_value=(MediaSource.TMDB, "286686"))
    monkeypatch.setattr(client, "_get_series_identity", get_series_identity, raising=False)
    monkeypatch.setattr(client, "get_remote_image_by_id", lambda **kwargs: None)

    event = client.get_webhook_message({
        "data": json.dumps({
            "Event": "playback.start",
            "Item": {
                "Type": item_type,
                "Id": "child-item-1",
                "Name": "第 24 集",
                "SeriesName": "生逢其时",
                "SeriesId": "series-item-1",
                "ProviderIds": {"Tmdb": "7750138"},
            },
        })
    }, {})

    assert event is not None
    get_series_identity.assert_called_once_with("series-item-1")
    assert event.item_id == "series-item-1"
    assert event.media_source == MediaSource.TMDB
    assert event.media_id == "286686"


def test_emby_tv_webhook_keeps_identity_empty_when_series_lookup_fails(monkeypatch) -> None:
    """Series 身份查询失败时不得回退为 Episode 或 Season 的 ProviderIds。"""
    client = Emby.__new__(Emby)
    monkeypatch.setattr(client, "_get_series_identity", Mock(return_value=(None, None)), raising=False)
    monkeypatch.setattr(client, "get_remote_image_by_id", lambda **kwargs: None)

    event = client.get_webhook_message({
        "data": json.dumps({
            "Event": "playback.start",
            "Item": {
                "Type": "Episode",
                "Id": "episode-item-1",
                "Name": "第 24 集",
                "SeriesId": "series-item-1",
                "ProviderIds": {"Tmdb": "7750138"},
            },
        })
    }, {})

    assert event is not None
    assert event.item_id == "series-item-1"
    assert event.media_source is None
    assert event.media_id is None


def test_emby_series_identity_requests_provider_ids(monkeypatch) -> None:
    """系列身份查询应只请求 Series 的 ProviderIds 并选择规范来源。"""
    client = Emby.__new__(Emby)
    client._host = "http://emby.local/"
    client._apikey = "test-key"
    client.user = "user-1"
    request = Mock()
    response = Mock(status_code=200)
    response.json.return_value = {"ProviderIds": {"Tmdb": "286686", "Tvdb": "468626"}}
    request.get_res.return_value = response
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils", lambda: request)

    identity = client._get_series_identity("series-item-1")

    assert identity == (MediaSource.TMDB, "286686")
    request.get_res.assert_called_once_with(
        "http://emby.local/emby/Users/user-1/Items/series-item-1",
        {"api_key": "test-key", "Fields": "ProviderIds"},
    )
