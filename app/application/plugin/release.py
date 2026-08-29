"""插件更新说明和 Release 版本查询用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from app.application.plugin.catalog import apply_declared_metadata_fallback
from app.application.plugin.identity import PluginIdentity
from app.schemas.plugin import Plugin

MarketPluginLoader = Callable[[str, str | None, bool], Awaitable[list[Plugin] | None]]
ReleaseCacheProbe = Callable[[str], Awaitable[bool]]
ReleaseLoader = Callable[[str, str], Awaitable[list[dict[str, Any]]]]
ReleaseRefresher = Callable[[str, str], Awaitable[object]]
IdentityReader = Callable[[str], Awaitable[PluginIdentity | None]]


@dataclass(frozen=True, slots=True)
class PluginReleaseSnapshot:
    """描述指定插件仓库的 Release 展示快照。"""

    release_supported: bool
    latest_version: str | None
    current_version: str | None
    items: tuple[dict[str, Any], ...]
    refresh_required: bool = False


class PluginReleaseService:
    """协调可信仓库元数据、Release 缓存和本地版本查询。"""

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], Sequence[Plugin]],
        local_repo_plugins: Callable[[], Sequence[Plugin]],
        market_plugins: MarketPluginLoader,
        local_version: Callable[[str], str | None],
        identity: IdentityReader,
        version_flag: Callable[[], str | None],
        compatible_flags: Callable[[str | None], Sequence[str]],
        has_release_cache: ReleaseCacheProbe,
        releases: ReleaseLoader,
        refresh_releases: ReleaseRefresher,
    ) -> None:
        """保存运行态、来源身份和市场读取窄端口。"""
        self._installed_plugins = installed_plugins
        self._local_repo_plugins = local_repo_plugins
        self._market_plugins = market_plugins
        self._local_version = local_version
        self._identity = identity
        self._version_flag = version_flag
        self._compatible_flags = compatible_flags
        self._has_release_cache = has_release_cache
        self._releases = releases
        self._refresh_releases = refresh_releases

    async def history(self, plugin_id: str, *, force: bool = True) -> Plugin | None:
        """按可信绑定仓库读取单个已安装插件的更新说明。"""
        installed_plugin = next(
            (plugin for plugin in self._installed_plugins() if plugin.id == plugin_id),
            None,
        )
        if installed_plugin is None:
            return None

        identity = await self._identity(plugin_id)
        if identity is not None:
            installed_plugin = apply_declared_metadata_fallback(
                [installed_plugin],
                {identity.normalized_plugin_id: identity},
            )[0]

        local_plugin = next(
            (plugin for plugin in self._local_repo_plugins() if plugin.id == plugin_id),
            None,
        )
        if local_plugin is not None:
            return _merge_market_metadata(installed_plugin, local_plugin)

        repo_url = _trusted_repo_url(identity)
        if repo_url is None:
            return installed_plugin
        market_plugin = await self._market_plugin(plugin_id, repo_url, force)
        return (
            _merge_market_metadata(installed_plugin, market_plugin)
            if market_plugin is not None
            else installed_plugin
        )

    async def versions(
        self,
        plugin_id: str,
        repo_url: str,
        *,
        force: bool = False,
    ) -> PluginReleaseSnapshot:
        """读取 Release 快照，并标记是否需要后台强制刷新已有缓存。"""
        if not repo_url:
            return PluginReleaseSnapshot(False, None, None, ())

        market_plugin = await self._market_plugin(plugin_id, repo_url, force)
        latest_version = market_plugin.plugin_version if market_plugin else None
        current_version = self._local_version(plugin_id)
        if not getattr(market_plugin, "release", False):
            return PluginReleaseSnapshot(
                False,
                latest_version,
                current_version,
                (),
            )

        refresh_required = force and await self._has_release_cache(repo_url)
        release_items = await self._releases(plugin_id, repo_url)
        items = tuple(
            _project_release_item(item, latest_version, current_version)
            for item in release_items
        )
        return PluginReleaseSnapshot(
            bool(items),
            latest_version,
            current_version,
            items,
            refresh_required=refresh_required,
        )

    async def refresh(self, plugin_id: str, repo_url: str) -> None:
        """强制刷新指定仓库的 Release 缓存。"""
        await self._refresh_releases(plugin_id, repo_url)

    async def _market_plugin(
        self, plugin_id: str, repo_url: str, force: bool
    ) -> Plugin | None:
        """按当前、兼容和基础索引顺序读取一个插件。"""
        version_flag = self._version_flag()
        package_versions: list[str | None] = [version_flag] if version_flag else []
        package_versions.extend(self._compatible_flags(version_flag))
        package_versions.append(None)
        for package_version in dict.fromkeys(package_versions):
            plugins = await self._market_plugins(repo_url, package_version, force)
            matched = next(
                (plugin for plugin in plugins or [] if plugin.id == plugin_id),
                None,
            )
            if matched is not None:
                return matched
        return None


_release_service: PluginReleaseService | None = None


def configure_plugin_release_service(service: PluginReleaseService) -> None:
    """由启动组合根发布当前 lifespan 的 Release 查询服务。"""
    global _release_service
    _release_service = service


def get_plugin_release_service() -> PluginReleaseService:
    """返回已经由启动组合根装配的 Release 查询服务。"""
    if _release_service is None:
        raise RuntimeError("插件 Release 查询服务尚未完成初始化")
    return _release_service


def reset_plugin_release_service() -> None:
    """清除当前 lifespan 的 Release 查询服务，供停机和隔离测试使用。"""
    global _release_service
    _release_service = None


def _trusted_repo_url(identity: PluginIdentity | None) -> str | None:
    """把持久化的 GitHub 来源键投影为仓库 URL。"""
    if identity is None or not identity.trusted_source_key:
        return None
    owner_repo = identity.trusted_source_key.removeprefix("github:")
    return f"https://github.com/{owner_repo}"


def _merge_market_metadata(plugin: Plugin, market_plugin: Plugin) -> Plugin:
    """在隔离副本上合并市场展示元数据。"""
    return cast(
        Plugin,
        plugin.model_copy(
            update={
                "repo_url": market_plugin.repo_url or plugin.repo_url,
                "history": market_plugin.history or {},
                "release": market_plugin.release,
                "has_update": market_plugin.has_update,
                "system_version": market_plugin.system_version
                or plugin.system_version,
                "system_version_compatible": market_plugin.system_version_compatible,
                "system_version_message": (
                    market_plugin.system_version_message
                    or plugin.system_version_message
                ),
            }
        ),
    )


def _project_release_item(
    item: Mapping[str, Any],
    latest_version: str | None,
    current_version: str | None,
) -> dict[str, Any]:
    """复制并标记一个 Release 条目的最新和当前版本状态。"""
    version = item.get("version")
    return {
        **item,
        "is_latest": bool(latest_version and version == latest_version),
        "is_current": bool(current_version and version == current_version),
    }
