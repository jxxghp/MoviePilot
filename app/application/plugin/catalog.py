"""插件市场目录应用服务。"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional, cast

from app.application.plugin.identity import (
    OFFICIAL_PLUGIN_SOURCE_KEY,
    PluginBindingBasis,
    PluginIdentity,
    TrustedPluginSourceType,
    normalize_physical_plugin_id,
)
from app.application.plugin.inventory import normalize_github_plugin_source
from app.schemas.plugin import (
    Plugin,
    PluginSourceBindingStatus,
    PluginUpdateCandidate,
)

MarketLoader = Callable[[str, Optional[str], bool], Optional[dict[str, dict]]]
AsyncMarketLoader = Callable[
    [str, Optional[str], bool],
    Awaitable[Optional[dict[str, dict]]],
]
PluginMapper = Callable[[str, dict, str, list[str], int, Optional[str]], Any]
ProgressCallback = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class _CatalogLoadRequest:
    """冻结同步与异步插件市场加载共用的仓库和代际参数。"""

    market: str
    package_version: Optional[str]
    force: bool


def apply_declared_metadata_fallback(
    plugins: Sequence[Plugin],
    identities: Mapping[str, PluginIdentity],
) -> list[Plugin]:
    """用已提交快照补齐加载失败插件，不覆盖真实运行态字段。"""
    result: list[Plugin] = []
    for plugin in plugins:
        identity = identities.get((plugin.id or "").lower())
        updates: dict[str, object] = {}
        if plugin.installed and not plugin.is_instance:
            if identity is None:
                updates["source_binding_status"] = PluginSourceBindingStatus.BINDING_REQUIRED
            elif identity.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
                updates["source_binding_status"] = (
                    PluginSourceBindingStatus.LOCAL_ONLY
                    if identity.binding_basis is PluginBindingBasis.LOCAL_ONLY
                    else PluginSourceBindingStatus.BINDING_REQUIRED
                )
            else:
                updates["source_binding_status"] = PluginSourceBindingStatus.BOUND
        if (
            identity is None
            or identity.declared_metadata is None
            or identity.declared_version is None
        ):
            result.append(plugin.model_copy(update=updates) if updates else plugin)
            continue
        fallback = identity.declared_metadata.display_fallback(
            installed_version=identity.declared_version
        )
        if not plugin.plugin_version:
            updates["plugin_version"] = fallback["plugin_version"]
        if (
            (not plugin.plugin_name or plugin.plugin_name == plugin.id)
            and "plugin_name" in fallback
        ):
            updates["plugin_name"] = fallback["plugin_name"]
        if not plugin.plugin_desc and "plugin_desc" in fallback:
            updates["plugin_desc"] = fallback["plugin_desc"]
        if not plugin.plugin_icon and "plugin_icon" in fallback:
            updates["plugin_icon"] = fallback["plugin_icon"]
        if not plugin.plugin_author and "plugin_author" in fallback:
            updates["plugin_author"] = fallback["plugin_author"]
        if not plugin.plugin_label and "plugin_label" in fallback:
            updates["plugin_label"] = fallback["plugin_label"]
        result.append(plugin.model_copy(update=updates) if updates else plugin)
    return result


class PluginCatalogService:
    """负责插件市场索引映射、并发收集、代际合并和来源去重。"""

    def __init__(
            self,
            *,
            market_loader: MarketLoader,
            async_market_loader: AsyncMarketLoader,
            installed_plugins_provider: Callable[[], list[str]],
            plugin_mapper: PluginMapper,
            is_local_repo: Callable[[Optional[str]], bool],
            version_compare: Callable[[str, str, str], bool],
            warning: Callable[[str], Any],
            error: Callable[[str], Any],
    ) -> None:
        """保存市场读取、插件映射和版本比较端口。"""
        self._market_loader = market_loader
        self._async_market_loader = async_market_loader
        self._installed_plugins_provider = installed_plugins_provider
        self._plugin_mapper = plugin_mapper
        self._is_local_repo = is_local_repo
        self._version_compare = version_compare
        self._warning = warning
        self._error = error

    @staticmethod
    def _load_request(
            market: str,
            package_version: Optional[str],
            force: bool,
    ) -> Optional[_CatalogLoadRequest]:
        """统一拒绝空市场，并冻结一次目录加载的完整输入。"""
        if not market:
            return None
        return _CatalogLoadRequest(
            market=market,
            package_version=package_version,
            force=force,
        )

    def _loaded_plugins(
            self,
            request: _CatalogLoadRequest,
            online_plugins: Optional[dict[str, dict[str, Any]]],
    ) -> list[Any]:
        """统一加载失败告警与在线目录到插件 DTO 的投影。"""
        if online_plugins is None:
            self._warning(
                f"获取{request.package_version if request.package_version else ''}插件库失败："
                f"{request.market}，请检查 GitHub 网络连接"
            )
            return []
        return self._map_plugins(
            online_plugins, request.market, request.package_version
        )

    def load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """同步读取并映射指定市场和插件代际。"""
        request = PluginCatalogService._load_request(
            market, package_version, force
        )
        if request is None:
            return []
        return self._loaded_plugins(
            request,
            self._market_loader(
                request.market, request.package_version, request.force
            ),
        )

    async def async_load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """异步读取并映射指定市场和插件代际。"""
        request = PluginCatalogService._load_request(
            market, package_version, force
        )
        if request is None:
            return []
        online_plugins = await self._async_market_loader(
            request.market,
            request.package_version,
            request.force,
        )
        return self._loaded_plugins(request, online_plugins)

    def collect(
            self,
            *,
            markets: list[str],
            compatible_flags: list[str],
            force: bool,
            loader: Callable[[str, Optional[str], bool], list[Any]],
    ) -> list[Any]:
        """并发读取多个市场和代际，并按稳定优先级合并。"""
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures_meta: dict[
                concurrent.futures.Future,
                tuple[int, bool, int],
            ] = {}
            for market_index, market in enumerate(markets):
                base_future = executor.submit(loader, market, None, force)
                futures_meta[base_future] = (market_index, False, 0)
                for flag_priority, flag in enumerate(compatible_flags):
                    higher_future = executor.submit(loader, market, flag, force)
                    futures_meta[higher_future] = (
                        market_index,
                        True,
                        flag_priority,
                    )

            collected = []
            for future in concurrent.futures.as_completed(futures_meta):
                plugins = future.result()
                market_index, is_higher, flag_priority = futures_meta[future]
                collected.append((
                    market_index,
                    is_higher,
                    flag_priority,
                    plugins or [],
                ))

        collected.sort(key=lambda item: (item[0], 0 if item[1] else 1, item[2]))
        higher_plugins = []
        base_plugins = []
        for _market_index, is_higher, _flag_priority, plugins in collected:
            (higher_plugins if is_higher else base_plugins).extend(plugins)
        return self.merge(higher_plugins, base_plugins, markets)

    async def async_collect(
            self,
            *,
            markets: list[str],
            compatible_flags: list[str],
            force: bool,
            loader: Callable[
                [str, Optional[str], bool],
                Awaitable[list[Any]],
            ],
            preserve_sources: bool = False,
            progress_callback: Optional[ProgressCallback] = None,
    ) -> list[Any]:
        """异步读取多个市场和代际，并按调用方需要合并或保留仓库候选。"""
        async def fetch(
                market: str,
                package_version: Optional[str],
                result_version: str,
                task_index: int,
        ) -> tuple[int, str, list[Any]]:
            """读取一个市场代际并保留创建时的稳定任务序号。"""
            plugins = await loader(market, package_version, force)
            return task_index, result_version, plugins or []

        tasks: list[asyncio.Task[tuple[int, str, list[Any]]]] = []
        for market in markets:
            tasks.append(asyncio.create_task(
                fetch(market, None, "base_version", len(tasks)),
                name="plugin.catalog.fetch",
            ))
            for flag in compatible_flags:
                tasks.append(asyncio.create_task(
                    fetch(market, flag, "higher_version", len(tasks)),
                    name="plugin.catalog.fetch",
                ))

        try:
            higher_plugins = []
            base_plugins = []
            if tasks:
                total_tasks = len(tasks)
                finished_tasks = 0
                task_results = {}
                if progress_callback:
                    progress_callback(
                        value=0,
                        text=f"开始刷新插件市场，共 {total_tasks} 个请求 ...",
                        data={"total": total_tasks, "finished": 0},
                    )
                for completed_task in asyncio.as_completed(tasks):
                    try:
                        task_index, version, plugins = await completed_task
                        task_results[task_index] = (version, plugins)
                    except Exception as err:
                        self._error(f"获取插件市场数据失败：{str(err)}")
                    finished_tasks += 1
                    if progress_callback:
                        progress_callback(
                            value=finished_tasks / total_tasks * 100,
                            text=(
                                f"插件市场请求（{finished_tasks}/{total_tasks}）"
                                "处理完成"
                            ),
                            data={"total": total_tasks, "finished": finished_tasks},
                        )
                for task_index in sorted(task_results):
                    version, plugins = task_results[task_index]
                    target = higher_plugins if version == "higher_version" else base_plugins
                    target.extend(plugins)

            result = (
                self.merge_by_source(higher_plugins, base_plugins, markets)
                if preserve_sources
                else self.merge(higher_plugins, base_plugins, markets)
            )
            if progress_callback:
                progress_callback(value=100, text="插件市场缓存刷新完成")
            return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def merge(
            self,
            higher_plugins: list[Any],
            base_plugins: list[Any],
            markets: list[str],
    ) -> list[Any]:
        """按代际、来源顺序和版本合并插件目录。"""
        all_plugins = list(higher_plugins)
        higher_keys = {
            (normalize_physical_plugin_id(plugin.id), plugin.plugin_version)
            for plugin in higher_plugins
        }
        all_plugins.extend(
            plugin
            for plugin in base_plugins
            if (
                normalize_physical_plugin_id(plugin.id),
                plugin.plugin_version,
            ) not in higher_keys
        )

        def repo_order(plugin: Any) -> int:
            """本地来源排在远程市场之后，远程来源保持配置顺序。"""
            if self._is_local_repo(plugin.repo_url):
                return len(markets) + 1
            if plugin.repo_url in markets:
                return markets.index(plugin.repo_url)
            return len(markets)

        deduplicated = {}
        for plugin in sorted(all_plugins, key=repo_order):
            key = (
                normalize_physical_plugin_id(plugin.id),
                plugin.plugin_version,
            )
            exists = deduplicated.get(key)
            if not exists or (
                self._is_local_repo(exists.repo_url)
                and not self._is_local_repo(plugin.repo_url)
            ):
                deduplicated[key] = plugin

        result_by_id = {}
        for plugin in sorted(deduplicated.values(), key=repo_order):
            normalized_id = normalize_physical_plugin_id(plugin.id)
            exists = result_by_id.get(normalized_id)
            if not exists \
                    or self._version_compare(
                        plugin.plugin_version,
                        ">",
                        exists.plugin_version,
                    ) \
                    or (
                        plugin.plugin_version == exists.plugin_version
                        and self._is_local_repo(exists.repo_url)
                        and not self._is_local_repo(plugin.repo_url)
                    ):
                result_by_id[normalized_id] = plugin
        return list(result_by_id.values())

    def merge_by_source(
            self,
            higher_plugins: list[Any],
            base_plugins: list[Any],
            markets: list[str],
    ) -> list[Any]:
        """每个仓库和代际保留同一插件的最高版本，供来源准入继续决策。"""
        higher_keys = {
            (
                plugin.repo_url,
                normalize_physical_plugin_id(plugin.id),
                plugin.plugin_version,
            )
            for plugin in higher_plugins
        }
        all_plugins = list(higher_plugins)
        all_plugins.extend(
            plugin
            for plugin in base_plugins
            if (
                plugin.repo_url,
                normalize_physical_plugin_id(plugin.id),
                plugin.plugin_version,
            ) not in higher_keys
        )

        def repo_order(plugin: Any) -> int:
            if plugin.repo_url in markets:
                return markets.index(plugin.repo_url)
            return len(markets)

        result_by_source: dict[tuple[Optional[str], str, Optional[str]], Any] = {}
        for plugin in sorted(all_plugins, key=repo_order):
            key = (
                plugin.repo_url,
                normalize_physical_plugin_id(plugin.id),
                plugin.package_version,
            )
            exists = result_by_source.get(key)
            if not exists or self._version_compare(
                plugin.plugin_version,
                ">",
                exists.plugin_version,
            ):
                result_by_source[key] = plugin
        return list(result_by_source.values())

    def _map_plugins(
            self,
            online_plugins: dict[str, dict],
            market: str,
            package_version: Optional[str],
    ) -> list[Any]:
        """把一个市场索引映射为宿主插件 DTO。"""
        installed_plugins = self._installed_plugins_provider()
        result = []
        add_time = len(online_plugins)
        for plugin_id, plugin_info in online_plugins.items():
            plugin = self._plugin_mapper(
                plugin_id,
                plugin_info,
                market,
                installed_plugins,
                add_time,
                package_version,
            )
            if plugin:
                result.append(plugin)
            add_time -= 1
        return result


class PluginCatalogQuery:
    """组合运行态、来源身份和市场候选，生成插件页目录快照。"""

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], list[Plugin]],
        local_plugins: Callable[[], list[Plugin]],
        local_repo_plugins: Callable[[], list[Plugin]],
        online_candidates: Callable[[bool], Awaitable[list[Plugin]]],
        process_plugins: Callable[[list[Plugin], list[Plugin]], list[Plugin]],
        identities: Callable[[list[str]], Awaitable[list[PluginIdentity]]],
    ) -> None:
        """保存运行态目录和来源身份读取窄端口。"""
        self._installed_plugins = installed_plugins
        self._local_plugins = local_plugins
        self._local_repo_plugins = local_repo_plugins
        self._online_candidates = online_candidates
        self._process_plugins = process_plugins
        self._identities = identities

    async def query(
        self, *, state: str = "all", force: bool = False
    ) -> list[Plugin]:
        """按 installed、market 或 all 返回插件页所需稳定目录。"""
        installed_plugins = self._installed_plugins()
        if state == "installed":
            return await self._with_declared_metadata(installed_plugins)

        local_plugins = self._local_plugins()
        not_installed = [plugin for plugin in local_plugins if not plugin.installed]
        local_repo_plugins = self._local_repo_plugins()
        online_plugins = await self._online_candidates(force)
        installed_ids = [plugin.id for plugin in installed_plugins if plugin.id]
        candidates = (
            self._process_plugins(online_plugins + local_repo_plugins, [])
            if online_plugins or local_repo_plugins
            else []
        )
        candidates = await self.project_update_candidates(
            online_plugins,
            candidates,
            installed_ids,
        )
        if not candidates:
            if state == "market":
                return not_installed
            return await self._with_declared_metadata(installed_plugins) + not_installed

        installed_keys = set(installed_ids)
        market_plugins = [
            plugin
            for plugin in candidates
            if plugin.id not in installed_keys or plugin.has_update
        ]
        market_ids = {plugin.id for plugin in market_plugins}
        market_plugins.extend(
            plugin for plugin in not_installed if plugin.id not in market_ids
        )
        if state == "market":
            return market_plugins
        return await self._with_declared_metadata(installed_plugins) + market_plugins

    async def project_update_candidates(
        self,
        online_plugins: list[Plugin],
        plugins: list[Plugin],
        installed_ids: list[str],
    ) -> list[Plugin]:
        """优先选择绑定仓库更新并投影候选来源信息。"""
        identities = await self._identities(installed_ids)
        identity_by_id = {item.normalized_plugin_id: item for item in identities}
        installed_keys = {plugin_id.lower() for plugin_id in installed_ids}
        result: list[Plugin] = []
        for plugin in plugins:
            if (
                not plugin.id
                or plugin.id.lower() not in installed_keys
                or not plugin.has_update
            ):
                result.append(plugin)
                continue
            identity = identity_by_id.get(plugin.id.lower())
            plugin = self._prefer_bound_update(plugin, online_plugins, identity)
            result.append(_project_update_candidate(plugin, identity))
        return result

    async def _with_declared_metadata(
        self, plugins: list[Plugin]
    ) -> list[Plugin]:
        """为未加载插件补齐已提交的展示声明。"""
        identities = await self._identities(
            [plugin.id for plugin in plugins if plugin.id]
        )
        return apply_declared_metadata_fallback(
            plugins,
            {identity.normalized_plugin_id: identity for identity in identities},
        )

    def _prefer_bound_update(
        self,
        plugin: Plugin,
        online_plugins: list[Plugin],
        identity: PluginIdentity | None,
    ) -> Plugin:
        """绑定仓库存在更新时优先返回其候选。"""
        if identity is None or not identity.trusted_source_key:
            return plugin
        bound_updates = [
            candidate
            for candidate in online_plugins
            if _matches_trusted_source(candidate, plugin.id, identity.trusted_source_key)
        ]
        preferred = self._process_plugins(bound_updates, []) if bound_updates else []
        return preferred[0] if preferred else plugin


_catalog_query: PluginCatalogQuery | None = None


def configure_plugin_catalog_query(query: PluginCatalogQuery) -> None:
    """由启动组合根发布当前 lifespan 的插件目录查询。"""
    global _catalog_query
    _catalog_query = query


def get_plugin_catalog_query() -> PluginCatalogQuery:
    """返回已经由启动组合根装配的插件目录查询。"""
    if _catalog_query is None:
        raise RuntimeError("插件目录查询尚未完成初始化")
    return _catalog_query


def reset_plugin_catalog_query() -> None:
    """清除当前 lifespan 的插件目录查询，供停机和隔离测试使用。"""
    global _catalog_query
    _catalog_query = None


def _matches_trusted_source(
    plugin: Plugin,
    plugin_id: str,
    trusted_source_key: str,
) -> bool:
    """判断市场候选是否为指定插件的可信绑定仓库更新。"""
    if (
        not plugin.id
        or plugin.id.lower() != plugin_id.lower()
        or not plugin.has_update
        or not plugin.repo_url
    ):
        return False
    try:
        source_key, _ = normalize_github_plugin_source(plugin.repo_url)
    except ValueError:
        return False
    return str(source_key) == trusted_source_key


def _project_update_candidate(
    plugin: Plugin, identity: PluginIdentity | None
) -> Plugin:
    """在隔离副本上附加来源类型和绑定关系。"""
    if not plugin.repo_url or not plugin.plugin_version:
        return plugin
    try:
        source_key, repo_url = normalize_github_plugin_source(plugin.repo_url)
    except ValueError:
        return plugin
    source_type = (
        TrustedPluginSourceType.OFFICIAL
        if source_key == OFFICIAL_PLUGIN_SOURCE_KEY
        else TrustedPluginSourceType.THIRD_PARTY
    )
    return cast(
        Plugin,
        plugin.model_copy(
        update={
            "update_candidate": PluginUpdateCandidate(
                source_type=source_type.value,
                source_key=source_key,
                repo_url=repo_url,
                version=plugin.plugin_version,
                is_bound=bool(
                    identity and identity.trusted_source_key == source_key
                ),
            )
        },
        ),
    )
