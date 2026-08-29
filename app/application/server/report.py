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
            async_subscribes_provider: Callable[[], Awaitable[list[Any]]] | None = None,
            plugin_report_sender: Callable[[list[dict]], Any],
            async_plugin_report_sender: Callable[[list[dict]], Awaitable[Any]],
            subscribe_report_sender: Callable[[list[dict]], Any],
            repo_url_sanitizer: Callable[[Optional[str]], Optional[str]],
            async_subscribe_report_sender: Callable[[list[dict]], Awaitable[Any]] | None = None,
            async_config_writer: Callable[[Any, Any], Awaitable[Any]] | None = None,
    ) -> None:
        """保存本地读取端口和只负责 I/O 的中心服务发送端口。"""
        self._config_reader = config_reader
        self._config_writer = config_writer
        self._installed_plugins_provider = installed_plugins_provider
        self._subscribes_provider = subscribes_provider
        self._async_subscribes_provider = async_subscribes_provider
        self._plugin_report_sender = plugin_report_sender
        self._async_plugin_report_sender = async_plugin_report_sender
        self._subscribe_report_sender = subscribe_report_sender
        self._async_subscribe_report_sender = async_subscribe_report_sender
        self._async_config_writer = async_config_writer
        self._repo_url_sanitizer = repo_url_sanitizer

    def init_report(
            self,
            *,
            enabled: bool,
            state_key: Any,
            reporter: Callable[[], bool],
    ) -> None:
        """首次成功上报后写入对应的完成标记。"""
        if self._should_initialize_report(enabled=enabled, state_key=state_key) and reporter():
            self._config_writer(state_key, "1")

    async def async_init_report(
        self,
        *,
        enabled: bool,
        state_key: Any,
        reporter: Callable[[], Awaitable[bool]],
    ) -> None:
        """异步完成首次上报，并通过异步配置端口持久化完成标记。"""
        if not self._should_initialize_report(enabled=enabled, state_key=state_key):
            return
        if not await reporter():
            return
        if self._async_config_writer is None:
            raise RuntimeError("中心服务上报未配置异步配置写入端口")
        await self._async_config_writer(state_key, "1")

    def _should_initialize_report(self, *, enabled: bool, state_key: Any) -> bool:
        """统一判断首次上报是否仍需执行。"""
        return enabled and not self._config_reader(state_key)

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

    def _prepare_subscribe_report(
        self,
        subscribes: list[Any],
    ) -> tuple[bool, Optional[list[dict[str, Any]]]]:
        """把订阅读取结果投影为无需发送的终态或待发送载荷。"""
        if not subscribes:
            return True, None
        payloads = [
            payload
            for subscribe in subscribes
            if (payload := self.build_subscribe_payload(subscribe.to_dict()))
        ]
        if not payloads:
            return True, None
        return False, payloads

    def _prepare_plugin_report(
        self,
        *,
        enabled: bool,
        items: Optional[list[tuple[str, Optional[str]]]],
    ) -> Optional[list[dict[str, Any]]]:
        """统一执行插件上报准入并构造脱敏载荷。"""
        if not enabled:
            return None
        payload = self.build_plugin_payload(items)
        return payload or None

    @staticmethod
    def _report_succeeded(response: Any) -> bool:
        """统一判定中心服务是否确认接收上报。"""
        return bool(response is not None and response.status_code == 200)

    def report_subscribes(self, *, enabled: bool) -> bool:
        """上报当前全部有效订阅的公开统计字段。"""
        if not enabled:
            return False
        subscribes = self._subscribes_provider()
        completed, payloads = self._prepare_subscribe_report(subscribes)
        if completed:
            return True
        assert payloads is not None
        response = self._subscribe_report_sender(payloads)
        return self._report_succeeded(response)

    def report_plugins(
            self,
            *,
            enabled: bool,
            items: Optional[list[tuple[str, Optional[str]]]] = None,
    ) -> bool:
        """同步上报当前插件安装清单。"""
        payload = self._prepare_plugin_report(enabled=enabled, items=items)
        if not payload:
            return False
        response = self._plugin_report_sender(payload)
        return self._report_succeeded(response)

    async def async_report_plugins(
            self,
            *,
            enabled: bool,
            items: Optional[list[tuple[str, Optional[str]]]] = None,
    ) -> bool:
        """异步上报当前插件安装清单。"""
        payload = self._prepare_plugin_report(enabled=enabled, items=items)
        if not payload:
            return False
        response = await self._async_plugin_report_sender(payload)
        return self._report_succeeded(response)

    async def async_report_subscribes(self, *, enabled: bool) -> bool:
        """异步上报当前全部有效订阅的公开统计字段。"""
        if not enabled:
            return False
        if self._async_subscribe_report_sender is None:
            raise RuntimeError("中心服务上报未配置异步订阅发送端口")
        if self._async_subscribes_provider is None:
            raise RuntimeError("中心服务未配置异步订阅读取端口")
        subscribes = await self._async_subscribes_provider()
        completed, payloads = self._prepare_subscribe_report(subscribes)
        if completed:
            return True
        assert payloads is not None
        response = await self._async_subscribe_report_sender(payloads)
        return self._report_succeeded(response)
