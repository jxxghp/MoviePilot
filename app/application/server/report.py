"""中心服务存量上报用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.schemas.media import resolve_media_identity


class ServerReportService:
    """协调本地订阅、插件清单和中心服务统计上报。"""

    SUBSCRIBE_FIELDS = frozenset({
        "name", "year", "type", "media_source", "media_id", "music_type",
        "total_tracks", "genre_ids", "season", "poster", "backdrop", "vote",
        "description",
    })

    def __init__(
            self,
            *,
            config_reader: Callable[[Any], Any],
            config_writer: Callable[[Any, Any], Any],
            installed_plugins_provider: Callable[[], list[str]],
            subscribes_provider: Callable[[], list[Any]],
            plugin_report_sender: Callable[[list[dict]], Any],
            async_plugin_report_sender: Callable[[list[dict]], Awaitable[Any]],
            subscribe_report_sender: Callable[[list[dict]], Any],
            repo_url_sanitizer: Callable[[Optional[str]], Optional[str]],
    ) -> None:
        """保存本地读取端口和只负责 I/O 的中心服务发送端口。"""
        self._config_reader = config_reader
        self._config_writer = config_writer
        self._installed_plugins_provider = installed_plugins_provider
        self._subscribes_provider = subscribes_provider
        self._plugin_report_sender = plugin_report_sender
        self._async_plugin_report_sender = async_plugin_report_sender
        self._subscribe_report_sender = subscribe_report_sender
        self._repo_url_sanitizer = repo_url_sanitizer

    def init_report(
            self,
            *,
            enabled: bool,
            state_key: Any,
            reporter: Callable[[], bool],
    ) -> None:
        """首次成功上报后写入对应的完成标记。"""
        if enabled and not self._config_reader(state_key) and reporter():
            self._config_writer(state_key, "1")

    def build_subscribe_payload(self, item: Optional[dict]) -> Optional[dict]:
        """构造中心服务订阅统计载荷并移除本地运行字段。"""
        if not isinstance(item, dict):
            return None
        media_source, media_id = resolve_media_identity(media=item)
        if not media_source or not media_id:
            return None
        payload = {
            key: value
            for key, value in item.items()
            if key in self.SUBSCRIBE_FIELDS
        }
        payload["media_source"] = str(media_source)
        payload["media_id"] = media_id
        return payload

    def build_plugin_payload(
            self,
            items: Optional[list[tuple[str, Optional[str]]]] = None,
    ) -> list[dict[str, Any]]:
        """构造插件安装统计载荷并脱敏本地仓库路径。"""
        if items:
            return [
                {
                    "plugin_id": plugin_id,
                    "repo_url": self._repo_url_sanitizer(repo_url),
                }
                for plugin_id, repo_url in items
                if plugin_id
            ]
        return [
            {"plugin_id": plugin_id, "repo_url": None}
            for plugin_id in self._installed_plugins_provider()
            if plugin_id
        ]

    def report_subscribes(self, *, enabled: bool) -> bool:
        """上报当前全部有效订阅的公开统计字段。"""
        if not enabled:
            return False
        subscribes = self._subscribes_provider()
        if not subscribes:
            return True
        payloads = [
            payload
            for subscribe in subscribes
            if (payload := self.build_subscribe_payload(subscribe.to_dict()))
        ]
        if not payloads:
            return True
        response = self._subscribe_report_sender(payloads)
        return bool(response is not None and response.status_code == 200)

    def report_plugins(
            self,
            *,
            enabled: bool,
            items: Optional[list[tuple[str, Optional[str]]]] = None,
    ) -> bool:
        """同步上报当前插件安装清单。"""
        if not enabled:
            return False
        payload = self.build_plugin_payload(items)
        if not payload:
            return False
        response = self._plugin_report_sender(payload)
        return bool(response is not None and response.status_code == 200)

    async def async_report_plugins(
            self,
            *,
            enabled: bool,
            items: Optional[list[tuple[str, Optional[str]]]] = None,
    ) -> bool:
        """异步上报当前插件安装清单。"""
        if not enabled:
            return False
        payload = self.build_plugin_payload(items)
        if not payload:
            return False
        response = await self._async_plugin_report_sender(payload)
        return bool(response is not None and response.status_code == 200)
