"""
插件市场目录应用服务。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional

from app.application.plugin.identity import (
    OFFICIAL_PLUGIN_SOURCE_KEY,
    PluginBindingBasis,
    PluginIdentity,
    TrustedPluginSourceType,
    normalize_physical_plugin_id,
)
from app.application.plugin.inventory import normalize_github_plugin_source
from app.domain.plugin import check_plugin_runtime_compatibility
from app.schemas.plugin import (
    Plugin,
    PluginDocumentation,
    PluginSourceBindingStatus,
    PluginUpdateCandidate,
)

MarketLoader = Callable[[str, Optional[str], bool], Optional[dict[str, dict[str, Any]]]]
AsyncMarketLoader = Callable[
    [str, Optional[str], bool],
    Awaitable[Optional[dict[str, dict[str, Any]]]],
]
PluginMapper = Callable[[str, dict[str, Any], str, list[str], int, Optional[str]], Any]
ProgressCallback = Callable[..., Any]
DEFAULT_MARKET_FETCH_CONCURRENCY = 5


class PluginMarketUnavailableError(RuntimeError):
    """
    有市场索引读取失败，且未取得可用候选，无法确认市场为空。
    """


@dataclass(frozen=True, slots=True)
class _CatalogIndexRead:
    """
    保留一次仓库索引读取的终态和投影结果。

    :param market: 配置市场地址
    :param package_version: 索引代际；None 表示基础索引
    :param plugins: 索引存在时映射出的候选，可以为空；None 表示没有可用索引
    :param error: 读取失败的原因；没有索引且无错误时表示已确认缺失
    """

    market: str
    package_version: Optional[str]
    plugins: Optional[list[Any]] = None
    error: Optional[str] = None

    @property
    def generation(self) -> str:
        """
        返回汇总日志使用的代际名称。
        """
        return self.package_version or "v1"

    @classmethod
    def failed(
        cls, market: str, package_version: Optional[str], error: Exception,
    ) -> _CatalogIndexRead:
        """
        记录读取失败，异常没有消息时也保留可诊断的类型。
        """
        return cls(market, package_version, error=str(error).strip() or type(error).__name__)


def apply_declared_metadata_fallback(
    plugins: Sequence[Plugin],
    identities: Mapping[str, PluginIdentity],
    *,
    free_threaded: bool = False,
) -> list[Plugin]:
    """
    用已提交快照补齐加载失败插件，不覆盖真实运行态字段。

    :param free_threaded: 当前是否运行在 free-threaded 解释器；已安装卡片的运行时
        不兼容提示只能来自安装时提交的声明快照，市场候选那条标注路径要联网才有。
    """
    result: list[Plugin] = []
    for plugin in plugins:
        identity = identities.get((plugin.id or "").lower())
        updates: dict[str, object] = {}
        if identity is not None and identity.declared_metadata is not None:
            compatible, message = check_plugin_runtime_compatibility(
                identity.declared_metadata.to_json().get("runtime"),
                free_threaded=free_threaded,
            )
            if not compatible:
                updates["runtime_compatible"] = False
                updates["runtime_message"] = message
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
    """
    负责插件市场索引映射、并发收集、刷新汇总和代际合并。
    """

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
            info: Callable[[str], Any],
            max_concurrency: int = DEFAULT_MARKET_FETCH_CONCURRENCY,
    ) -> None:
        """
        保存市场读取、插件映射和日志回调。索引不存在时读取端口返回 None，失败时抛出异常。
        """
        if max_concurrency < 1:
            raise ValueError("插件市场读取并发必须大于 0")
        self._market_loader = market_loader
        self._async_market_loader = async_market_loader
        self._installed_plugins_provider = installed_plugins_provider
        self._plugin_mapper = plugin_mapper
        self._is_local_repo = is_local_repo
        self._version_compare = version_compare
        self._warning = warning
        self._error = error
        self._info = info
        self._max_concurrency = max_concurrency

    def _index_read(
            self,
            market: str,
            package_version: Optional[str],
            payload: Optional[dict[str, dict[str, Any]]],
    ) -> _CatalogIndexRead:
        """
        只映射确实存在的索引，保留有效空索引与缺失索引的区别。
        """
        return _CatalogIndexRead(
            market, package_version,
            None if payload is None else self._map_plugins(payload, market, package_version),
        )

    def _read_index(
            self,
            market: str,
            package_version: Optional[str],
            force: bool,
    ) -> _CatalogIndexRead:
        """
        同步读取单个索引，收集预期的传输或解析失败，不提前写日志。
        """
        try:
            payload = self._market_loader(market, package_version, force)
        except (ValueError, RuntimeError) as error:
            return _CatalogIndexRead.failed(market, package_version, error)
        return self._index_read(market, package_version, payload)

    async def _async_read_index(
            self,
            market: str,
            package_version: Optional[str],
            force: bool,
    ) -> _CatalogIndexRead:
        """
        异步读取单个索引，保留成功、缺失和失败三种结果供目录使用。
        """
        try:
            payload = await self._async_market_loader(market, package_version, force)
        except (ValueError, RuntimeError) as error:
            return _CatalogIndexRead.failed(market, package_version, error)
        return self._index_read(market, package_version, payload)

    def _single_index_plugins(self, read: _CatalogIndexRead) -> list[Any]:
        """
        单独查询某一代索引时就地记录读取失败，仍返回兼容的空列表。
        """
        if read.error is not None:
            self._warning(
                f"获取 {read.generation} 插件索引失败：{read.market}，{read.error}"
            )
        return read.plugins or []

    def load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """
        同步读取并映射指定市场和插件代际。
        """
        if not market:
            return []
        return self._single_index_plugins(self._read_index(market, package_version, force))

    async def async_load(
            self,
            market: str,
            package_version: Optional[str] = None,
            force: bool = False,
    ) -> list[Any]:
        """
        异步读取指定市场，沿用单仓目录读取失败时返回空列表的约定。

        :param market: 插件市场仓库地址
        :param package_version: 要读取的索引代际；None 表示基础索引
        :param force: 是否跳过读取缓存
        :return: 映射后的插件目录，读取失败时为空
        """
        if not market:
            return []
        return self._single_index_plugins(await self._async_read_index(market, package_version, force))

    def _log_index_refresh(
            self,
            reads: Sequence[_CatalogIndexRead],
            compatible_flags: Sequence[str],
            plugin_count: int,
    ) -> None:
        """
        汇总一轮索引探测，只为没有任何可用索引的仓库写单独错误。

        :param reads: 按配置仓库和代际读取顺序排列的终态
        :param compatible_flags: 兼容代际按优先级排列；最后才考虑基础索引
        :param plugin_count: 合并后的线上插件数
        """
        if not reads:
            return
        reads_by_market: dict[str, list[_CatalogIndexRead]] = {}
        for read in reads:
            reads_by_market.setdefault(read.market, []).append(read)
        missing = Counter(read.generation for read in reads if read.plugins is None and read.error is None)
        failures = Counter(read.generation for read in reads if read.error is not None)
        generations = [*compatible_flags, "v1"]
        available = 0
        fallback = 0
        for market, market_reads in reads_by_market.items():
            present = {read.generation for read in market_reads if read.plugins is not None}
            if present:
                available += 1
                fallback += generations[0] not in present
                continue
            details = "；".join(f"{read.generation} {read.error or '未发布'}" for read in market_reads)
            self._error(f"插件仓库未取得任何索引：{market}（{details}）")
        total = len(reads_by_market)
        self._info(
            f"插件索引探测完成：共 {total} 个仓库，{available} 个可用，{total - available} 个无可用索引，"
            f"{fallback} 个回退到较低代际；缺少索引 {self._format_index_counts(missing, generations)}；"
            f"读取失败 {self._format_index_counts(failures, generations)}；获取到 {plugin_count} 个线上插件"
        )

    @staticmethod
    def _format_index_counts(counts: Counter[str], generations: Sequence[str]) -> str:
        """
        按代际优先级呈现探测次数，省略为零的代际。
        """
        details = " / ".join(f"{generation} {counts[generation]}" for generation in generations if counts[generation])
        return f"{counts.total()} 次（{details}）" if details else "0 次"

    def collect(
            self,
            *,
            markets: list[str],
            compatible_flags: list[str],
            force: bool,
    ) -> list[Any]:
        """
        并发读取多个市场及代际，汇总本轮结果并按稳定优先级合并。
        """
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_concurrency
        ) as executor:
            futures_meta: dict[
                concurrent.futures.Future[_CatalogIndexRead],
                tuple[int, str, Optional[str]],
            ] = {}
            for market in markets:
                base_future = executor.submit(self._read_index, market, None, force)
                futures_meta[base_future] = (len(futures_meta), market, None)
                for flag in compatible_flags:
                    higher_future = executor.submit(self._read_index, market, flag, force)
                    futures_meta[higher_future] = (len(futures_meta), market, flag)

            collected: dict[int, _CatalogIndexRead] = {}
            for future in concurrent.futures.as_completed(futures_meta):
                index, market, generation = futures_meta[future]
                try:
                    collected[index] = future.result()
                # 投影等意外异常也不能中断整个刷新
                except Exception as error:  # noqa: BLE001
                    collected[index] = _CatalogIndexRead.failed(market, generation, error)

        reads = [collected[index] for index in sorted(collected)]
        higher_plugins, base_plugins = self._split_index_plugins(reads)
        result = self.merge(higher_plugins, base_plugins, markets)
        self._log_index_refresh(reads, compatible_flags, len(result))
        return result

    @staticmethod
    def _split_index_plugins(reads: Sequence[_CatalogIndexRead]) -> tuple[list[Any], list[Any]]:
        """
        按原有顺序拆出高代际和基础索引的候选，供目录合并。
        """
        higher_plugins: list[Any] = []
        base_plugins: list[Any] = []
        for read in reads:
            target = higher_plugins if read.package_version is not None else base_plugins
            target.extend(read.plugins or [])
        return higher_plugins, base_plugins

    @staticmethod
    def _require_available_market_candidates(
        *,
        required: bool,
        candidates: list[Any],
        expected_reads: int,
        successful_reads: int,
    ) -> None:
        """
        在市场页候选为空且读取不完整时保留上游依赖错误。

        :param required: 调用方是否需要确认市场状态
        :param candidates: 已成功读取仓库映射后的候选
        :param expected_reads: 本轮预期的仓库和代际读取数
        :param successful_reads: 已成功返回可判定索引的读取数
        :raises PluginMarketUnavailableError: 存在失败读取且没有可展示候选
        """
        if required and not candidates and successful_reads < expected_reads:
            raise PluginMarketUnavailableError(
                "插件市场索引暂时无法读取，请检查 GitHub 网络连接和代理配置"
            )

    async def async_collect(
            self,
            *,
            markets: list[str],
            compatible_flags: list[str],
            force: bool,
            preserve_sources: bool = False,
            require_available: bool = False,
            progress_callback: Optional[ProgressCallback] = None,
    ) -> list[Any]:
        """
        异步读取多个市场和代际，并按调用方需要合并或保留仓库候选。

        :param markets: 本轮读取的仓库地址，顺序决定合并优先级
        :param compatible_flags: 要读取的插件代际及其优先级
        :param force: 是否跳过索引缓存
        :param preserve_sources: 是否保留每个仓库的候选，不跨仓去重
        :param require_available: 是否在没有候选且有索引读取失败时报告市场不可用
        :param progress_callback: 可选的进度更新回调
        :return: 合并或保留来源后的插件目录
        :raises PluginMarketUnavailableError: 要求确认市场可用，但存在失败且没有候选
        """
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def fetch(
                market: str,
                package_version: Optional[str],
                task_index: int,
        ) -> tuple[int, _CatalogIndexRead]:
            """
            读取一个市场代际并保留创建时的稳定任务序号。
            """
            async with semaphore:
                try:
                    read = await self._async_read_index(market, package_version, force)
                # 投影等意外异常也不能中断整个刷新
                except Exception as error:  # noqa: BLE001
                    read = _CatalogIndexRead.failed(market, package_version, error)
            return task_index, read

        tasks: list[asyncio.Task[tuple[int, _CatalogIndexRead]]] = []
        for market in markets:
            tasks.append(asyncio.create_task(
                fetch(market, None, len(tasks)),
                name="plugin.catalog.fetch",
            ))
            for flag in compatible_flags:
                tasks.append(asyncio.create_task(
                    fetch(market, flag, len(tasks)),
                    name="plugin.catalog.fetch",
                ))

        try:
            task_results: dict[int, _CatalogIndexRead] = {}
            if tasks:
                total_tasks = len(tasks)
                finished_tasks = 0
                if progress_callback:
                    progress_callback(
                        value=0,
                        text=f"开始刷新插件市场，共 {total_tasks} 个请求 ...",
                        data={"total": total_tasks, "finished": 0},
                    )
                for completed_task in asyncio.as_completed(tasks):
                    task_index, read = await completed_task
                    task_results[task_index] = read
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
            reads = [task_results[index] for index in sorted(task_results)]
            higher_plugins, base_plugins = self._split_index_plugins(reads)
            result = (
                self.merge_by_source(higher_plugins, base_plugins, markets)
                if preserve_sources
                else self.merge(higher_plugins, base_plugins, markets)
            )
            self._log_index_refresh(reads, compatible_flags, len(result))
            self._require_available_market_candidates(
                required=require_available,
                candidates=result,
                expected_reads=len(tasks),
                successful_reads=sum(read.error is None for read in reads),
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
        """
        按代际、来源顺序和版本合并插件目录。
        """
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
            """
            本地来源排在远程市场之后，远程来源保持配置顺序。
            """
            if self._is_local_repo(plugin.repo_url):
                return len(markets) + 1
            if plugin.repo_url in markets:
                return markets.index(plugin.repo_url)
            return len(markets)

        deduplicated: dict[tuple[Optional[str], Optional[str]], Any] = {}
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

        result_by_id: dict[str, Any] = {}
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
                        and not self._is_local_repo(exists.repo_url)
                        and self._is_local_repo(plugin.repo_url)
                    ):
                result_by_id[normalized_id] = plugin
        return list(result_by_id.values())

    def merge_by_source(
            self,
            higher_plugins: list[Any],
            base_plugins: list[Any],
            markets: list[str],
    ) -> list[Any]:
        """
        每个仓库和代际保留同一插件的最高版本，供来源准入继续决策。
        """
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
            """
            保留配置的仓库顺序，未配置来源排在末尾。
            """
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
            online_plugins: dict[str, dict[str, Any]],
            market: str,
            package_version: Optional[str],
    ) -> list[Any]:
        """
        把一个市场索引映射为宿主插件 DTO。
        """
        installed_plugins = self._installed_plugins_provider()
        result: list[Any] = []
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
    """
    组合运行态、来源身份和市场候选，生成插件页目录快照。
    """

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], list[Plugin]],
        local_plugins: Callable[[], list[Plugin]],
        local_repo_plugins: Callable[[], list[Plugin]],
        online_candidates: Callable[[bool], Awaitable[list[Plugin]]],
        process_plugins: Callable[[list[Plugin], list[Plugin]], list[Plugin]],
        identities: Callable[[list[str]], Awaitable[list[PluginIdentity]]],
        free_threaded: Callable[[], bool],
        readme_url: Optional[Callable[[str, str, Optional[str]], Awaitable[Optional[str]]]] = None,
        source_candidates: Optional[Callable[[str], Awaitable[list[Plugin]]]] = None,
    ) -> None:
        """
        保存运行态目录、来源身份读取和解释器变体判定窄端口。

        :param source_candidates: 只读取所选配置仓库的候选；未装配时不回退全市场扫描
        """
        self._installed_plugins = installed_plugins
        self._local_plugins = local_plugins
        self._local_repo_plugins = local_repo_plugins
        self._online_candidates = online_candidates
        self._process_plugins = process_plugins
        self._identities = identities
        self._free_threaded = free_threaded
        self._readme_url = readme_url
        self._source_candidates = source_candidates

    async def documentation(self, plugin_id: str, repo_url: str) -> PluginDocumentation:
        """
        为所选市场来源返回显式说明链接，缺省时按候选的真实代际探测 README。

        :param plugin_id: 源插件 ID，不要求已安装
        :param repo_url: 用户当前查看或选择安装的仓库；本地来源只使用显式链接
        :return: 文档入口；不在目录中的来源、缺失文档均返回空入口
        """
        if repo_url.startswith("local://"):
            candidates = self._local_repo_plugins()
        elif self._source_candidates is not None:
            candidates = await self._source_candidates(repo_url)
        else:
            return PluginDocumentation()
        matches = [
            plugin for plugin in candidates
            if (plugin.id or "").lower() == plugin_id.lower()
            and (plugin.repo_url or "").rstrip("/").lower() == repo_url.rstrip("/").lower()
        ]
        # 同一仓库可能同时提供多代索引，沿用市场卡片的版本与代际选择规则。
        candidate = next(iter(self._process_plugins(matches, [])), None)
        if candidate is None:
            return PluginDocumentation()
        if candidate.plugin_readme:
            return PluginDocumentation(url=candidate.plugin_readme)
        if self._readme_url is None or repo_url.startswith("local://"):
            return PluginDocumentation()
        return PluginDocumentation(
            url=await self._readme_url(candidate.id or plugin_id, candidate.repo_url or repo_url, candidate.package_version),
        )

    async def query(
        self, *, state: str = "all", force: bool = False
    ) -> list[Plugin]:
        """
        按 installed、market 或 all 返回插件页所需稳定目录。

        :param state: 目录范围；market 失败时传播市场不可用状态
        :param force: 是否强制刷新在线目录
        :return: 按所选范围筛选后的插件目录
        :raises PluginMarketUnavailableError: 市场视图没有在线候选且有索引读取失败
        """
        installed_plugins = self._installed_plugins()
        if state == "installed":
            return await self._with_declared_metadata(installed_plugins)

        local_plugins = self._local_plugins()
        not_installed = [plugin for plugin in local_plugins if not plugin.installed]
        local_repo_plugins = self._local_repo_plugins()
        try:
            online_plugins = await self._online_candidates(force)
        except PluginMarketUnavailableError:
            if state == "market" and not local_repo_plugins and not not_installed:
                raise
            online_plugins = []
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
        """
        已绑定插件仅提示绑定仓库更新，并投影候选来源信息。
        """
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
        """
        为未加载插件补齐已提交的展示声明。
        """
        identities = await self._identities(
            [plugin.id for plugin in plugins if plugin.id]
        )
        return apply_declared_metadata_fallback(
            plugins,
            {identity.normalized_plugin_id: identity for identity in identities},
            free_threaded=self._free_threaded(),
        )

    def _prefer_bound_update(
        self,
        plugin: Plugin,
        online_plugins: list[Plugin],
        identity: PluginIdentity | None,
    ) -> Plugin:
        """
        按绑定来源收敛更新标记，不修改共享的市场候选快照。
        """
        if identity is None or not identity.trusted_source_key or not plugin.id:
            return plugin
        trusted_source_key = identity.trusted_source_key
        plugin_id = plugin.id
        bound_updates = [
            candidate
            for candidate in online_plugins
            if _matches_trusted_source(candidate, plugin_id, trusted_source_key)
        ]
        preferred = self._process_plugins(bound_updates, []) if bound_updates else []
        return preferred[0] if preferred else plugin.model_copy(update={"has_update": False})


_catalog_query: PluginCatalogQuery | None = None


def configure_plugin_catalog_query(query: PluginCatalogQuery) -> None:
    """
    由启动组合根发布当前 lifespan 的插件目录查询。
    """
    global _catalog_query
    _catalog_query = query


def get_plugin_catalog_query() -> PluginCatalogQuery:
    """
    返回已经由启动组合根装配的插件目录查询。
    """
    if _catalog_query is None:
        raise RuntimeError("插件目录查询尚未完成初始化")
    return _catalog_query


def reset_plugin_catalog_query() -> None:
    """
    清除当前 lifespan 的插件目录查询，供停机和隔离测试使用。
    """
    global _catalog_query
    _catalog_query = None


def _matches_trusted_source(
    plugin: Plugin,
    plugin_id: str,
    trusted_source_key: str,
) -> bool:
    """
    判断市场候选是否为指定插件的可信绑定仓库更新。
    """
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
    """
    在隔离副本上附加来源类型和绑定关系。
    """
    if not plugin.repo_url or not plugin.plugin_version:
        return plugin
    try:
        source_key, repo_url = normalize_github_plugin_source(plugin.repo_url)
    except ValueError:
        return plugin
    source_type: Literal["official", "third_party"] = (
        "official" if source_key == OFFICIAL_PLUGIN_SOURCE_KEY else "third_party"
    )
    return plugin.model_copy(
            update={
                "update_candidate": PluginUpdateCandidate(
                    source_type=source_type,
                    source_key=source_key,
                    repo_url=repo_url,
                    version=plugin.plugin_version,
                    is_bound=bool(
                        identity and identity.trusted_source_key == source_key
                    ),
                )
            },
        )
