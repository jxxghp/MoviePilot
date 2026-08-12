import json

import pytest

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.zspace.zspace import ZSpace
from app.schemas import WebhookEventInfo
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
