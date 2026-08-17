"""插件市场目录应用服务。"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from typing import Any, Optional


MarketLoader = Callable[[str, Optional[str], bool], Optional[dict[str, dict]]]
AsyncMarketLoader = Callable[
    [str, Optional[str], bool],
    Awaitable[Optional[dict[str, dict]]],
]
PluginMapper = Callable[[str, dict, str, list[str], int, Optional[str]], Any]
ProgressCallback = Callable[..., Any]


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

    def load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """同步读取并映射指定市场和插件代际。"""
        if not market:
            return []
        online_plugins = self._market_loader(market, package_version, force)
        if online_plugins is None:
            self._warning(
                f"获取{package_version if package_version else ''}插件库失败："
                f"{market}，请检查 GitHub 网络连接"
            )
            return []
        return self._map_plugins(online_plugins, market, package_version)

    async def async_load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """异步读取并映射指定市场和插件代际。"""
        if not market:
            return []
        online_plugins = await self._async_market_loader(
            market,
            package_version,
            force,
        )
        if online_plugins is None:
            self._warning(
                f"获取{package_version if package_version else ''}插件库失败："
                f"{market}，请检查 GitHub 网络连接"
            )
            return []
        return self._map_plugins(online_plugins, market, package_version)

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
            progress_callback: Optional[ProgressCallback] = None,
    ) -> list[Any]:
        """异步读取多个市场和代际，并持续报告稳定进度。"""
        async def fetch(
                market: str,
                package_version: Optional[str],
                result_version: str,
                task_index: int,
        ) -> tuple[int, str, list[Any]]:
            """读取一个市场代际并保留创建时的稳定任务序号。"""
            plugins = await loader(market, package_version, force)
            return task_index, result_version, plugins or []

        tasks = []
        for market in markets:
            tasks.append(asyncio.create_task(
                fetch(market, None, "base_version", len(tasks))
            ))
            for flag in compatible_flags:
                tasks.append(asyncio.create_task(
                    fetch(market, flag, "higher_version", len(tasks))
                ))

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
                (higher_plugins if version == "higher_version" else base_plugins).extend(
                    plugins
                )

        result = self.merge(higher_plugins, base_plugins, markets)
        if progress_callback:
            progress_callback(value=100, text="插件市场缓存刷新完成")
        return result

    def merge(
            self,
            higher_plugins: list[Any],
            base_plugins: list[Any],
            markets: list[str],
    ) -> list[Any]:
        """按代际、来源顺序和版本合并插件目录。"""
        all_plugins = list(higher_plugins)
        higher_keys = {
            f"{plugin.id}{plugin.plugin_version}"
            for plugin in higher_plugins
        }
        all_plugins.extend(
            plugin
            for plugin in base_plugins
            if f"{plugin.id}{plugin.plugin_version}" not in higher_keys
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
            key = f"{plugin.id}{plugin.plugin_version}"
            exists = deduplicated.get(key)
            if not exists or (
                self._is_local_repo(exists.repo_url)
                and not self._is_local_repo(plugin.repo_url)
            ):
                deduplicated[key] = plugin

        result_by_id = {}
        for plugin in sorted(deduplicated.values(), key=repo_order):
            exists = result_by_id.get(plugin.id)
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
                result_by_id[plugin.id] = plugin
        return list(result_by_id.values())

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
