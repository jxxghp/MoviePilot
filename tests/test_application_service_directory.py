"""应用层服务配置目录测试。"""

from app.application import service
from app.application.mediaserver import get_mediaserver_configs
from app.application.notification import (
    get_notification_configs,
    get_notification_switch,
)
from app.schemas.system import (
    MediaServerConf,
    NotificationConf,
    NotificationSwitchConf,
)
from app.schemas.types import MessageType, SystemConfigKey


def test_named_service_config_helpers_preserve_enabled_policy(monkeypatch) -> None:
    """命名应用函数应复用同一配置目录，并显式控制禁用项可见性。"""
    configs = {
        SystemConfigKey.MediaServers: [
            MediaServerConf(name="enabled-media", type="plex", enabled=True),
            MediaServerConf(name="disabled-media", type="emby", enabled=False),
        ],
        SystemConfigKey.Notifications: [
            NotificationConf(name="enabled-channel", type="telegram", enabled=True),
            NotificationConf(name="disabled-channel", type="wechat", enabled=False),
        ],
        SystemConfigKey.NotificationSwitchs: [
            NotificationSwitchConf(
                type=MessageType.Download.value,
                action="admin",
            )
        ],
    }
    monkeypatch.setattr(
        service,
        "_config_loader",
        lambda config_key, _conf_type: configs.get(config_key, []),
    )

    assert [item.name for item in get_mediaserver_configs()] == ["enabled-media"]
    assert [
        item.name for item in get_mediaserver_configs(include_disabled=True)
    ] == ["enabled-media", "disabled-media"]
    assert [item.name for item in get_notification_configs()] == ["enabled-channel"]
    assert [
        item.name for item in get_notification_configs(include_disabled=True)
    ] == ["enabled-channel", "disabled-channel"]
    assert get_notification_switch(MessageType.Download) == "admin"
