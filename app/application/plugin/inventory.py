"""插件市场候选库存读取与外部事实映射。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, TypeAlias
from urllib.parse import unquote, urlsplit

from app.application.plugin.identity import (
    OFFICIAL_PLUGIN_SOURCE_KEY,
    TrustedPluginSourceType,
    validate_online_source_key,
)
from app.application.plugin.source import (
    CandidateInventory,
    LocalCandidateRead,
    MarketRead,
    PluginLocalCandidate,
    PluginMarketCandidate,
    normalize_package_generation,
)
from app.foundation.environment import is_free_threaded_runtime

PLUGIN_V3_GENERATIONS = ("v3", "v2", "v1")
PluginIndex: TypeAlias = Mapping[str, Mapping[str, Any]]
PluginIndexLoaderResult: TypeAlias = PluginIndex | None
LocalCandidateLoadPayload: TypeAlias = (
    Mapping[str, Mapping[str, Any]]
    | Iterable[Mapping[str, Any]]
    | None
)
MarketLoader: TypeAlias = Callable[
    [str, str | None, bool],
    PluginIndexLoaderResult,
]
AsyncMarketLoader: TypeAlias = Callable[
    [str, str | None, bool],
    Awaitable[PluginIndexLoaderResult],
]
LocalCandidateLoader: TypeAlias = Callable[
    [],
    LocalCandidateLoadPayload,
]


class PluginCandidateInventoryReader:
    """按配置市场和 V3 代际顺序保留全部候选及读取终态。"""

    def __init__(
        self,
        *,
        market_loader: MarketLoader,
        local_candidate_loader: LocalCandidateLoader | None = None,
        async_market_loader: AsyncMarketLoader | None = None,
        generations: Sequence[str] = PLUGIN_V3_GENERATIONS,
        max_concurrency: int = 24,
    ) -> None:
        """保存读取端口，并限制异步市场请求的进程内并发。"""
        normalized_generations = tuple(
            normalize_package_generation(generation)
            for generation in generations
        )
        if normalized_generations != PLUGIN_V3_GENERATIONS:
            raise ValueError("V3 候选库存必须按 v3、v2、v1 顺序读取")
        if max_concurrency < 1:
            raise ValueError("插件市场读取并发必须大于 0")
        self._market_loader = market_loader
        self._async_market_loader = async_market_loader
        self._local_candidate_loader = local_candidate_loader
        self._generations = normalized_generations
        self._max_concurrency = max_concurrency

    def load(
        self,
        markets: Iterable[str],
        *,
        force: bool = False,
    ) -> CandidateInventory:
        """同步读取全部配置市场，不把失败市场伪装成空仓库。"""
        normalized_markets = _normalize_markets(markets)
        reads = tuple(
            self._read_market_generation(market, generation, force=force)
            for market in normalized_markets
            for generation in self._generations
        )
        local_read = self._load_local_candidates()
        return CandidateInventory(
            reads,
            local_read.candidates,
            local_read=local_read,
            expected_markets=normalized_markets,
            expected_generations=self._generations,
        )

    async def async_load(
        self,
        markets: Iterable[str],
        *,
        force: bool = False,
    ) -> CandidateInventory:
        """有界并发读取全部市场，同时保持配置与代际的稳定顺序。"""
        normalized_markets = _normalize_markets(markets)
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def read(market: str, generation: str) -> MarketRead:
            async with semaphore:
                return await self._async_read_market_generation(
                    market,
                    generation,
                    force=force,
                )

        tasks = [
            asyncio.create_task(
                read(market, generation),
                name="plugin.inventory.read",
            )
            for market in normalized_markets
            for generation in self._generations
        ]
        try:
            reads = tuple(await asyncio.gather(*tasks)) if tasks else ()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        local_read = await asyncio.to_thread(self._load_local_candidates)
        return CandidateInventory(
            reads,
            local_read.candidates,
            local_read=local_read,
            expected_markets=normalized_markets,
            expected_generations=self._generations,
        )

    def _read_market_generation(
        self,
        market: str,
        generation: str,
        *,
        force: bool,
    ) -> MarketRead:
        """同步读取一个市场代际并映射为候选事实。"""
        try:
            source_key, repo_url, source_type = _market_source(market)
            payload = self._market_loader(
                repo_url,
                _package_version(generation),
                force,
            )
            return _successful_read(
                market,
                generation,
                payload,
                source_key=source_key,
                source_type=source_type,
                repo_url=repo_url,
            )
        except Exception as error:  # noqa: BLE001 - 失败事实必须进入快照
            return MarketRead.failure(
                market,
                _error_message(error),
                package_generation=generation,
            )

    async def _async_read_market_generation(
        self,
        market: str,
        generation: str,
        *,
        force: bool,
    ) -> MarketRead:
        """异步读取一个市场代际并映射为候选事实。"""
        try:
            source_key, repo_url, source_type = _market_source(market)
            if self._async_market_loader is None:
                payload = await asyncio.to_thread(
                    self._market_loader,
                    repo_url,
                    _package_version(generation),
                    force,
                )
            else:
                payload = await self._async_market_loader(
                    repo_url,
                    _package_version(generation),
                    force,
                )
            return _successful_read(
                market,
                generation,
                payload,
                source_key=source_key,
                source_type=source_type,
                repo_url=repo_url,
            )
        except Exception as error:  # noqa: BLE001 - 失败事实必须进入快照
            return MarketRead.failure(
                market,
                _error_message(error),
                package_generation=generation,
            )

    def _load_local_candidates(self) -> LocalCandidateRead:
        """映射本地候选，并保留扫描失败而非伪装为空仓库。"""
        if self._local_candidate_loader is None:
            return LocalCandidateRead.absent()
        try:
            raw_candidates = self._local_candidate_loader()
            if raw_candidates is None:
                return LocalCandidateRead.failure(
                    "本地插件仓库读取未返回可判定结果"
                )
        except Exception as error:  # noqa: BLE001 - 失败事实必须进入快照
            return LocalCandidateRead.failure(_error_message(error))
        entries: Iterable[tuple[object, Mapping[str, Any]]]
        try:
            if isinstance(raw_candidates, Mapping):
                entries = raw_candidates.items()
            else:
                entries = (
                    (plugin_info.get("id"), plugin_info)
                    for plugin_info in raw_candidates
                    if isinstance(plugin_info, Mapping)
                )

            candidates: list[PluginLocalCandidate] = []
            for plugin_id, plugin_info in entries:
                if not isinstance(plugin_id, str) or not isinstance(plugin_info, Mapping):
                    continue
                try:
                    candidate = _local_candidate(plugin_id, plugin_info)
                except (TypeError, ValueError):
                    continue
                if candidate is not None:
                    candidates.append(candidate)
        except Exception as error:  # noqa: BLE001 - 迭代或映射失败也要保留状态
            return LocalCandidateRead.failure(_error_message(error))
        return LocalCandidateRead.present(candidates)


def build_plugin_candidate_inventory(
    markets: Iterable[str],
    *,
    market_loader: MarketLoader,
    local_candidate_loader: LocalCandidateLoader | None = None,
    force: bool = False,
) -> CandidateInventory:
    """使用注入的同步读取端口构建一次候选库存。"""
    return PluginCandidateInventoryReader(
        market_loader=market_loader,
        local_candidate_loader=local_candidate_loader,
    ).load(markets, force=force)


def normalize_github_plugin_source(value: str) -> tuple[str, str]:
    """把 GitHub 仓库 URL 或来源键归一为持久来源键和公开地址。"""
    normalized = str(value).strip().rstrip("/")
    if normalized.lower().startswith("github:"):
        source_key = validate_online_source_key(normalized)
        owner, repository = source_key.removeprefix("github:").split("/", 1)
        return source_key, f"https://github.com/{owner}/{repository}"

    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("插件市场必须是 GitHub 仓库地址")
    if parsed.hostname.lower() != "github.com":
        raise ValueError("插件市场必须使用 github.com 仓库地址")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("插件市场 GitHub 地址缺少 owner 或 repository")
    owner, repository = parts[:2]
    repository = repository.removesuffix(".git")
    source_key = validate_online_source_key(f"github:{owner}/{repository}")
    return source_key, f"https://github.com/{owner}/{repository}"


def _normalize_markets(markets: Iterable[str]) -> tuple[str, ...]:
    """规范化并去除重复市场，保留无效配置供读取快照报错。"""
    result: list[str] = []
    seen: set[str] = set()
    for market in markets:
        value = str(market).strip().rstrip("/")
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _market_source(
    market: str,
) -> tuple[str, str, TrustedPluginSourceType]:
    """解析 GitHub 市场来源，并固定官方仓库分类。"""
    source_key, repo_url = normalize_github_plugin_source(market)
    source_type = (
        TrustedPluginSourceType.OFFICIAL
        if source_key == OFFICIAL_PLUGIN_SOURCE_KEY
        else TrustedPluginSourceType.THIRD_PARTY
    )
    return source_key, repo_url, source_type


def _successful_read(
    market: str,
    generation: str,
    payload: PluginIndexLoaderResult,
    *,
    source_key: str,
    source_type: TrustedPluginSourceType,
    repo_url: str,
) -> MarketRead:
    """把 Adapter 读取结果映射为一个市场代际的三态事实。"""
    if payload is None:
        return MarketRead.absent(
            market,
            package_generation=generation,
        )
    if not isinstance(payload, Mapping):
        raise TypeError("插件市场索引必须是对象")
    return MarketRead.present(
        market,
        _market_candidates(
            payload,
            source_key=source_key,
            source_type=source_type,
            repo_url=repo_url,
            package_generation=generation,
        ),
        package_generation=generation,
    )


def _market_candidates(
    payload: PluginIndex,
    *,
    source_key: str,
    source_type: TrustedPluginSourceType,
    repo_url: str,
    package_generation: str,
) -> tuple[PluginMarketCandidate, ...]:
    """过滤并映射一个代际索引中的 V3 可兼容条目。"""
    result: list[PluginMarketCandidate] = []
    for index_plugin_id, raw_info in payload.items():
        if not isinstance(raw_info, Mapping):
            continue
        plugin_id = raw_info.get("id") or index_plugin_id
        if not isinstance(plugin_id, str):
            continue
        if not _is_v3_compatible(raw_info, package_generation):
            continue
        plugin_version = raw_info.get("version")
        if plugin_version is None:
            plugin_version = raw_info.get("plugin_version")
        if plugin_version == "":
            plugin_version = None
        try:
            result.append(
                PluginMarketCandidate(
                    plugin_id=plugin_id,
                    source_key=source_key,
                    source_type=source_type,
                    repo_url=repo_url,
                    package_generation=package_generation,
                    plugin_version=plugin_version,
                    dto=dict(raw_info),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _is_v3_compatible(
    plugin_info: Mapping[str, Any],
    package_generation: str,
) -> bool:
    """按宿主 V3、兼容 V2、基础索引顺序判断候选兼容性。"""
    if plugin_info.get("v3") is False:
        return False
    if is_free_threaded_runtime() and plugin_info.get("v3t") is False:
        return False
    if package_generation in {"v3", "v2"}:
        return True
    return plugin_info.get("v3") is True or plugin_info.get("v2") is True


def _local_candidate(
    plugin_id: str,
    plugin_info: Mapping[str, Any],
) -> PluginLocalCandidate | None:
    """把本地插件索引条目转换为应用候选。"""
    generation = normalize_package_generation(
        str(
            plugin_info.get("package_version")
            or plugin_info.get("package_generation")
            or "v1"
        )
    )
    if not _is_v3_compatible(plugin_info, generation):
        return None
    repo_url = plugin_info.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.startswith("local://"):
        return None
    plugin_version = plugin_info.get("version")
    if plugin_version is None:
        plugin_version = plugin_info.get("plugin_version")
    if plugin_version == "":
        plugin_version = None
    return PluginLocalCandidate(
        plugin_id=plugin_id,
        repo_url=repo_url,
        package_generation=generation,
        plugin_version=plugin_version,
        dto=dict(plugin_info),
    )


def _package_version(package_generation: str) -> str | None:
    """把公共代际转换为市场索引文件参数。"""
    return None if package_generation == "v1" else package_generation


def _error_message(error: Exception) -> str:
    """保留可诊断的读取失败说明，并避免空异常丢失状态。"""
    message = str(error).strip()
    return message or error.__class__.__name__
