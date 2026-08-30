"""通知渠道稳定身份与缓存同步契约。"""

import pytest

from app.api.endpoints.notification import _normalize_configs
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.schemas.system import NotificationConf


def test_notification_config_normalizes_names_and_rejects_duplicates():
    configs = [NotificationConf(name=" Alpha ", type="telegram")]
    assert _normalize_configs(configs)[0]["name"] == "Alpha"

    with pytest.raises(ValueError, match="重复"):
        _normalize_configs(
            [
                NotificationConf(name="Alpha", type="telegram"),
                NotificationConf(name=" alpha ", type="wechat"),
            ]
        )


def test_reconcile_cached_states_migrates_renamed_identity_and_cleans_removed(monkeypatch):
    values = {}

    class FakeCache:
        def exists(self, key):
            return key in values

        def get(self, key):
            return values.get(key)

        def set(self, key, value):
            values[key] = value

        def delete(self, key):
            values.pop(key, None)

    monkeypatch.setattr(
        "app.modules.wechatclawbot.wechatclawbot.FileCache", FakeCache
    )
    old_name = WechatClawBot._build_cache_key("旧名")
    removed_id = WechatClawBot._build_cache_key("removed-id")
    values[old_name] = b"state"
    values[removed_id] = b"stale"

    result = WechatClawBot.reconcile_cached_states(
        [
            {"id": "stable-id", "name": "旧名", "type": "wechatclawbot"},
            {"id": "removed-id", "name": "已删除", "type": "wechatclawbot"},
        ],
        [{"id": "stable-id", "name": "新名", "type": "wechatclawbot"}],
    )

    assert result["success"] is True
    assert values[WechatClawBot._build_cache_key("stable-id")] == b"state"
    assert old_name not in values
    assert removed_id not in values


@pytest.mark.parametrize("identity", ["stable-id", "display-name"])
def test_cache_key_is_stable_for_same_identity(identity):
    assert WechatClawBot._build_cache_key(identity) == WechatClawBot._build_cache_key(identity)
