"""媒体服务器客户端安全投影接口测试。"""

import asyncio

from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.schemas.types import SystemConfigKey


def test_media_server_clients_only_projects_enabled_names_and_types(monkeypatch) -> None:
    """客户端列表不得返回地址、令牌或其他完整连接配置。"""
    calls = []

    class FakeSystemConfig:
        """返回包含敏感字段的媒体服务器配置。"""

        def get(self, key):
            """记录读取键并返回测试配置。"""
            calls.append(key)
            return [
                {
                    "name": "家庭 Emby",
                    "type": "emby",
                    "enabled": True,
                    "config": {"host": "https://example.invalid", "token": "secret"},
                },
                {
                    "name": "停用 Plex",
                    "type": "plex",
                    "enabled": False,
                    "config": {"token": "disabled-secret"},
                },
            ]

    monkeypatch.setattr(
        mediaserver_endpoint,
        "get_configured_system_config",
        lambda: FakeSystemConfig(),
    )

    result = asyncio.run(mediaserver_endpoint.clients(_=object()))

    assert result == [{"name": "家庭 Emby", "type": "emby"}]
    assert calls == [SystemConfigKey.MediaServers]
