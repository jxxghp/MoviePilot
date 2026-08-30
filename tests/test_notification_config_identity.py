"""通知渠道稳定身份与缓存同步契约。"""

import pytest

from app.api.endpoints.notification import _normalize_configs, save_config
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


@pytest.mark.asyncio
async def test_save_config_does_not_touch_cache_when_persistence_fails(monkeypatch):
    calls = {"reconcile": 0}

    class FakeConfig:
        def get(self, _key):
            return []

        async def async_set(self, _key, _value):
            return False

    monkeypatch.setattr(
        "app.api.endpoints.notification.get_configured_system_config",
        lambda: FakeConfig(),
    )

    def fail_if_reconciled(_previous, _current):
        calls["reconcile"] += 1
        return {"success": True}

    monkeypatch.setattr(WechatClawBot, "reconcile_cached_states", fail_if_reconciled)
    response = await save_config([NotificationConf(name="Alpha", type="telegram")], None)

    assert response.success is False
    assert response.message == "通知配置保存失败"
    assert calls["reconcile"] == 0


@pytest.mark.asyncio
async def test_save_config_saves_channels_without_loaded_clawbot_module(monkeypatch):
    calls = []

    class FakeConfig:
        def get(self, _key):
            return []

        async def async_set(self, _key, value):
            calls.append(value)
            return True

    monkeypatch.setattr(
        "app.api.endpoints.notification.get_configured_system_config",
        lambda: FakeConfig(),
    )
    monkeypatch.setattr(
        WechatClawBot,
        "reconcile_cached_states",
        lambda _previous, _current: {"success": True},
    )

    response = await save_config([NotificationConf(name="TG", type="telegram")], None)

    assert response.success is True
    assert response.data["value"][0]["name"] == "TG"
    assert len(calls) == 1
