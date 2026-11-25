"""
通知渠道稳定身份与缓存同步契约。
"""

import pytest

from app.api.endpoints.notification import _normalize_configs, save_config
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.schemas.system import NotificationConf


def test_notification_config_normalizes_names_and_rejects_duplicates():
    """
    保存渠道配置前清理名称空白，并按不区分大小写的身份规则拒绝重名。
    """
    configs = [NotificationConf(name=" Alpha ", type="telegram")]
    assert _normalize_configs(configs)[0]["name"] == "Alpha"

    with pytest.raises(ValueError, match="重复"):
        _normalize_configs(
            [
                NotificationConf(name="Alpha", type="telegram"),
                NotificationConf(name=" alpha ", type="wechat"),
            ]
        )


def test_notification_channel_switches_keep_string_list_contract():
    """
    渠道场景开关仍按消息类型名称列表存储并参与匹配。
    """
    config = NotificationConf(
        name="Alpha",
        type="telegram",
        switchs=["资源下载"],
    )

    assert config.switchs == ["资源下载"]


def test_reconcile_cached_states_migrates_renamed_identity_and_cleans_removed(monkeypatch):
    """
    渠道改名时迁移对应会话缓存，并清理已删除渠道的缓存。
    """
    values = {}

    class FakeCache:
        """
        用内存字典记录通知会话缓存，避免接触真实缓存文件。
        """
        def exists(self, key):
            """
            检查测试缓存是否保存指定会话。
            """
            return key in values

        def get(self, key):
            """
            读取测试缓存，未保存时返回 None。
            """
            return values.get(key)

        def set(self, key, value):
            """
            把会话写入本用例的内存缓存。
            """
            values[key] = value

        def delete(self, key):
            """
            移除测试缓存中的会话，不存在时保持不变。
            """
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
    """
    同一渠道身份始终生成相同缓存键，保证会话可重复定位。
    """
    assert WechatClawBot._build_cache_key(identity) == WechatClawBot._build_cache_key(identity)


@pytest.mark.asyncio
async def test_save_config_does_not_touch_cache_when_persistence_fails(monkeypatch):
    """
    配置持久化失败时不提前迁移或清理渠道会话缓存。
    """
    calls = {"reconcile": 0}

    class FakeConfig:
        """
        模拟通知配置读写结果，隔离真实系统配置存储。
        """
        def get(self, _key):
            """
            返回空的既有渠道配置。
            """
            return []

        async def async_set(self, _key, _value):
            """
            模拟配置写入失败，验证缓存不会提前更新。
            """
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
    """
    微信机器人模块尚未加载时仍可正常保存渠道配置。
    """
    calls = []

    class FakeConfig:
        """
        模拟通知配置读写结果，隔离真实系统配置存储。
        """
        def get(self, _key):
            """
            返回空的既有渠道配置。
            """
            return []

        async def async_set(self, _key, value):
            """
            记录本次渠道配置写入并返回成功。
            """
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
