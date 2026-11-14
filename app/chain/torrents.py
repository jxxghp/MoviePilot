"""
站点种子刷新、预识别缓存和订阅候选准备。
"""

import re
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Union, cast

from app.application.configuration import get_configured_system_config
from app.application.rss import RssHelper
from app.application.site.observation import SiteSearchObservation, capture_site_search_observation
from app.application.site.protocol import site_domain, torrent_resource_identity

# 站点资源包在启动时加载，静态检查环境可能无法解析其实现。
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.site.throttle import site_request_cooldown
from app.application.subscription.candidates import CandidateIndex
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.torrent.download import TorrentHelper
from app.chain.base import ChainBase
from app.chain.media import MediaChain
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.foundation.url import UrlUtils
from app.runtime.cache import cache_mutation
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import resolve_media_identity
from app.schemas.message import Message
from app.schemas.types import MediaType, MessageType, NotificationChannel, SystemConfigKey


class TorrentsChain(ChainBase):
    """
    站点首页或RSS种子处理链，服务于订阅、刷流等。

    :cvar _refresh_sites_lock: 保护站点刷新准入集合的进程锁
    :cvar _refreshing_sites: 仍在抓取或识别的站点，真实结束前不释放
    """

    _refresh_sites_lock = threading.Lock()
    _refreshing_sites: set[int] = set()

    _spider_file = "__torrents_cache__"
    _rss_file = "__rss_cache__"
    # 音乐资源独立缓存，与影视种子分开计算配额与存储，避免被影视资源挤出
    _music_spider_file = "__torrents_music_cache__"
    _music_rss_file = "__rss_music_cache__"

    def _cache_type(self, stype: Optional[str]) -> str:
        """
        把可选缓存类型统一为本次读取使用的明确订阅模式。
        """
        return stype or self.runtime_config.subscribe_mode

    def _cache_file_pair(self, stype: str) -> tuple[str, str]:
        """
        返回指定订阅模式下影视与音乐缓存的唯一文件映射。
        """
        if stype == 'spider':
            return self._spider_file, self._music_spider_file
        return self._rss_file, self._music_rss_file

    @staticmethod
    def _merge_torrent_caches(
        torrents_cache: Dict[str, List[Context]],
        music_cache: Dict[str, List[Context]],
    ) -> Dict[str, List[Context]]:
        """
        按站点稳定合并音乐独立缓存，空列表不创建无效站点键。
        """
        for domain, contexts in music_cache.items():
            if contexts:
                torrents_cache.setdefault(domain, []).extend(contexts)
        return torrents_cache

    @property
    def cache_file(self) -> str:
        """
        返回缓存文件列表
        """
        if self.runtime_config.subscribe_mode == 'spider':
            return self._spider_file
        return self._rss_file

    def remote_refresh(
        self, channel: NotificationChannel, userid: Optional[Union[str, int]] = None
    ) -> None:
        """
        远程刷新订阅，发送消息
        """
        self.post_message(Message(
            channel=channel,
            title="开始刷新种子 ...",
            userid=userid,
            save_history=False))
        self.refresh()
        self.post_message(Message(
            channel=channel,
            title="种子刷新完成！",
            userid=userid,
            save_history=False))

    def get_torrents(self, stype: Optional[str] = None) -> Dict[str, List[Context]]:
        """
        获取当前缓存的种子，包含独立缓存的音乐资源

        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """

        stype = self._cache_type(stype)
        video_file, _music_file = self._cache_file_pair(stype)
        torrents_cache = self.load_cache(video_file) or {}

        # 兼容性处理：为旧版本的Context对象补齐新增候选识别字段
        self._ensure_context_compatibility(torrents_cache, stype=stype)

        # 合并音乐独立缓存，供订阅匹配等消费方按站点读取完整候选
        music_cache = self.get_music_torrents(stype=stype)
        return self._merge_torrent_caches(torrents_cache, music_cache)

    def get_music_torrents(self, stype: Optional[str] = None) -> Dict[str, List[Context]]:
        """
        获取音乐独立缓存的种子

        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """
        stype = self._cache_type(stype)
        _video_file, music_file = self._cache_file_pair(stype)
        music_cache = self.load_cache(music_file) or {}
        # 兼容性处理：为旧版本的Context对象补齐新增候选识别字段
        self._ensure_context_compatibility(music_cache, stype=stype)
        return music_cache

    def cache_files(self, stype: Optional[str] = None) -> tuple[str, str]:
        """
        返回影视与音乐缓存文件名，供按当前订阅模式回写各自缓存

        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """
        return self._cache_file_pair(self._cache_type(stype))

    @staticmethod
    def split_cache_contexts(
            torrents_cache: Dict[str, List[Context]],
    ) -> tuple[Dict[str, List[Context]], Dict[str, List[Context]]]:
        """
        将合并读取的缓存按种子分类拆分为影视缓存与音乐缓存，用于分别回写各自存储文件。
        """
        video_cache: Dict[str, List[Context]] = {}
        music_cache: Dict[str, List[Context]] = {}
        for domain, contexts in torrents_cache.items():
            for context in contexts:
                torrent = context.torrent_info
                if torrent and torrent.category in (MediaType.MUSIC, MediaType.MUSIC.value):
                    music_cache.setdefault(domain, []).append(context)
                else:
                    video_cache.setdefault(domain, []).append(context)
        return video_cache, music_cache

    async def async_get_torrents(self, stype: Optional[str] = None) -> Dict[str, List[Context]]:
        """
        异步获取当前缓存的种子，包含独立缓存的音乐资源

        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """

        stype = self._cache_type(stype)
        video_file, music_file = self._cache_file_pair(stype)
        torrents_cache = await self.async_load_cache(video_file) or {}
        music_cache = await self.async_load_cache(music_file) or {}

        # 兼容性处理：为旧版本的Context对象补齐新增候选识别字段
        self._ensure_context_compatibility(torrents_cache, stype=stype)
        self._ensure_context_compatibility(music_cache, stype=stype)

        # 合并音乐独立缓存，供订阅匹配等消费方按站点读取完整候选
        return self._merge_torrent_caches(torrents_cache, music_cache)

    def get_subscribe_cache_candidates(
            self,
            subscribe: SubscriptionSnapshot,
            stype: Optional[str] = None,
            allow_title_match: bool = False,
    ) -> List[Context]:
        """
        按订阅身份读取 RSS/spider 缓存候选，返回不会回写缓存的 Context 副本。

        主程序只提供缓存读取与轻量候选筛选，不在这里判断站点证据能否扩展
        订阅目标或放行完成；标题兜底候选会显式标记为低置信来源。
        """
        candidates = {
            domain: [context for context in contexts or [] if context]
            for domain, contexts in (self.get_torrents(stype=stype) or {}).items()
        }
        return CandidateIndex(candidates).select_cache_candidates(
            subscribe,
            allow_title_match=allow_title_match,
        )

    @classmethod
    def _context_matches_subscribe(
        cls, context: Context, subscribe: SubscriptionSnapshot
    ) -> bool:
        """
        严格身份匹配：候选自身识别出的媒体 ID 命中订阅，且季信息不排除订阅季。
        """
        return CandidateIndex.strict_matches(context, subscribe)

    @classmethod
    def _context_title_matches_subscribe(
        cls, context: Context, subscribe: SubscriptionSnapshot
    ) -> bool:
        """
        标题兜底只服务诊断：仅允许身份缺失候选按标题命中，显式冲突 ID 不兜底。
        """
        return CandidateIndex.title_matches(context, subscribe)

    @staticmethod
    def _mark_title_match_candidate(
        context: Context, subscribe: SubscriptionSnapshot
    ) -> None:
        """
        标记标题兜底候选，避免下游把目标媒体回填误认为候选自身识别结果。
        """
        CandidateIndex.mark_title_candidate(context, subscribe)

    @classmethod
    def _context_media_type_matches(
        cls, context: Context, subscribe: SubscriptionSnapshot
    ) -> bool:
        """
        类型已知且冲突时拒绝；缺失类型不作为缓存候选过滤条件。
        """
        return CandidateIndex.media_type_matches(context, subscribe)

    @classmethod
    def _context_season_matches_subscribe(
        cls, context: Context, subscribe: SubscriptionSnapshot
    ) -> bool:
        """
        资源季信息只要明确排除订阅季就拒绝；跨季覆盖目标季留给插件诊断。
        """
        return CandidateIndex.season_matches(context, subscribe)

    @classmethod
    def _context_meta_seasons(cls, meta_info: Optional[MetaBase]) -> set[int]:
        """
        提取标题解析出的显式季范围；多季包以该范围为准。
        """
        return CandidateIndex.meta_seasons(meta_info)

    @staticmethod
    def _context_has_media_identity(context: Context) -> bool:
        """
        判断候选是否已经带有明确媒体 ID。
        """
        return bool(CandidateIndex.context_identities(context))

    @staticmethod
    def _context_media_identities(context: Context) -> set[tuple[str, str]]:
        """
        提取候选媒体信息与标题标签中的通用媒体身份。
        """
        return CandidateIndex.context_identities(context)

    @staticmethod
    def _normalize_int(value: object) -> Optional[int]:
        """
        将季号等动态字段转为 int，无法解析时视为缺失。
        """
        return CandidateIndex.normalize_int(value)

    @staticmethod
    def _normalize_media_type(value: object) -> Optional[str]:
        """
        统一 MediaType 枚举与字符串形态。
        """
        return CandidateIndex.normalize_media_type(value)

    @staticmethod
    def _normalize_title(value: object) -> str:
        """
        归一标题用于低置信标题兜底匹配。
        """
        return CandidateIndex.normalize_title(value)

    def clear_torrents(self) -> None:
        """
        清理种子缓存数据，包含音乐独立缓存
        """
        logger.info('开始清理种子缓存数据 ...')
        self.remove_cache(self._spider_file)
        self.remove_cache(self._rss_file)
        self.remove_cache(self._music_spider_file)
        self.remove_cache(self._music_rss_file)
        logger.info('种子缓存数据清理完成')

    async def async_clear_torrents(self) -> None:
        """
        异步清理种子缓存数据，包含音乐独立缓存
        """
        logger.info('开始异步清理种子缓存数据 ...')
        await self.async_remove_cache(self._spider_file)
        await self.async_remove_cache(self._rss_file)
        await self.async_remove_cache(self._music_spider_file)
        await self.async_remove_cache(self._music_rss_file)
        logger.info('异步种子缓存数据清理完成')

    def browse(self, domain: str, keyword: Optional[str] = None, cat: Optional[str] = None,
               page: Optional[int] = 0,
               mtype: Optional[MediaType] = None) -> List[TorrentInfo]:
        """
        浏览站点首页内容，返回种子清单，TTL缓存5分钟

        :param domain: 站点域名
        :param keyword: 搜索标题
        :param cat: 搜索分类
        :param page: 页码
        :param mtype: 媒体类型
        """
        logger.info(f'开始获取站点 {domain} 最新种子 ...')
        site = SitesHelper().get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return self.refresh_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    async def async_browse(self, domain: str, keyword: Optional[str] = None, cat: Optional[str] = None,
                           page: Optional[int] = 0,
                           mtype: Optional[MediaType] = None) -> List[TorrentInfo]:
        """
        异步浏览站点首页内容，返回种子清单，TTL缓存5分钟

        :param domain: 站点域名
        :param keyword: 搜索标题
        :param cat: 搜索分类
        :param page: 页码
        :param mtype: 媒体类型
        """
        logger.info(f'开始获取站点 {domain} 最新种子 ...')
        site = await SitesHelper().async_get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return await self.async_refresh_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    def rss(self, domain: str) -> List[TorrentInfo]:
        """
        获取站点RSS内容，返回种子清单，TTL缓存3分钟

        :param domain: 站点域名
        """
        logger.info(f'开始获取站点 {domain} RSS ...')
        site = SitesHelper().get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        rss_url = UrlUtils.normalize_http_url(site.get("rss"))
        if rss_url is None:
            logger.warning(f'站点 {domain} RSS地址无效，跳过获取')
            return []
        rss_items = RssHelper().parse(rss_url, True if site.get("proxy") else False,
                                      timeout=int(site.get("timeout") or 30),
                                      ua=site.get("ua") if site.get("ua") else None)
        if rss_items is None:
            # rss过期，尝试保留原配置生成新的rss
            self.__renew_rss_url(domain=domain, site=site)
            return []
        if not isinstance(rss_items, list):
            logger.error(f'站点 {domain} RSS返回格式无效！')
            return []
        if not rss_items:
            logger.error(f'站点 {domain} 未获取到RSS数据！')
            return []
        # 站点级媒体类型，用于给缺少分类信息的 RSS 种子补充分类
        media_type_value = site.get("media_type")
        site_media_type = (
            MediaType.from_agent(media_type_value)
            if isinstance(media_type_value, str) else None
        )
        ret_torrents: List[TorrentInfo] = []
        try:
            for item in rss_items:
                if not item.get("title"):
                    continue
                torrentinfo = TorrentInfo(
                    site=site.get("id"),
                    site_name=site.get("name"),
                    site_cookie=site.get("cookie"),
                    site_ua=site.get("ua") or self.runtime_config.user_agent,
                    site_proxy=bool(site.get("proxy")),
                    site_order=int(site.get("pri") or 0),
                    site_downloader=site.get("downloader"),
                    title=item.get("title"),
                    enclosure=item.get("enclosure"),
                    page_url=item.get("link"),
                    size=float(item.get("size") or 0),
                    pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
                    # RSS 报文不带站点分类，按站点媒体类型补充，否则音乐资源无法进入音乐订阅匹配
                    category=site_media_type.value if site_media_type else None,
                )
                ret_torrents.append(torrentinfo)
        finally:
            rss_items.clear()
            del rss_items
        return ret_torrents

    @staticmethod
    def _music_browse_paths(site: dict[str, Any]) -> List[str]:
        """
        返回站点独立于默认浏览入口的音乐种子页面路径。

        部分站点的默认种子列表只显示电影和电视剧，音乐需要单独的菜单页面进入；
        这类站点在索引配置中用 type=music 的搜索路径声明音乐入口。
        默认入口已覆盖音乐（音乐站点或未定义独立入口）时返回空列表。
        """
        # 音乐站点全站都是音乐资源，默认浏览入口已经覆盖
        media_type_value = site.get("media_type")
        if isinstance(media_type_value, str) and MediaType.from_agent(media_type_value) == MediaType.MUSIC:
            return []
        paths = (site.get("search") or {}).get("paths") or []
        if len(paths) <= 1:
            return []
        # 计算默认浏览使用的路径，与其相同的音乐入口无需重复抓取
        browse_conf = site.get("browse") or {}
        default_path = browse_conf.get("path")
        if not default_path:
            default_path = next(
                (item.get("path") for item in paths if item.get("type") in (None, "all")),
                paths[0].get("path"),
            )
        return [
            item.get("path") for item in paths
            if item.get("type") == "music"
            and item.get("path")
            and item.get("path") != default_path
        ]

    def __append_music_browse_torrents(
            self,
            domain: str,
            torrents: List[TorrentInfo],
            observations: list[SiteSearchObservation],
    ) -> List[TorrentInfo]:
        """
        追加音乐专用入口并去重，逐页保存请求事实，避免后页限流抹掉前页的真实请求。
        """
        seen = {torrent.enclosure for torrent in torrents if torrent.enclosure}
        for page in range(2):
            with capture_site_search_observation() as observation:
                page_torrents = self.browse(domain=domain, page=page, mtype=MediaType.MUSIC)
            observations.append(observation)
            if not page_torrents:
                # 某一页没有数据，说明已经到最后一页，停止获取
                break
            for torrent in page_torrents:
                if torrent.enclosure and torrent.enclosure in seen:
                    continue
                if torrent.enclosure:
                    seen.add(torrent.enclosure)
                torrents.append(torrent)
        return torrents

    def _fetch_refresh_resources(
            self, indexer: dict[str, Any], stype: str, include_music: bool,
    ) -> tuple[List[TorrentInfo], dict[str, object]]:
        """
        在共享缓存锁外抓取站点页面，区分成功空页、失败和未发请求的限流。
        """
        domain = site_domain(indexer.get("domain"))
        torrents: List[TorrentInfo] = []
        observations = []
        for page in range(2 if stype == "spider" else 1):
            with capture_site_search_observation() as observation:
                page_torrents = self.browse(domain=domain, page=page) if stype == "spider" else self.rss(domain=domain)
            observations.append(observation)
            torrents.extend(page_torrents)
            if not page_torrents or runtime_stop_state.is_system_stopped:
                break
        if include_music and self._music_browse_paths(indexer) and not runtime_stop_state.is_system_stopped:
            torrents = self.__append_music_browse_torrents(domain, torrents, observations)
        reported = [item for item in observations if item.outcome != "skipped"]
        # RSS 和旧插件不报告观察事实时保留未知，不把空列表当作成功。
        outcome = "success" if torrents else "unknown"
        if reported:
            outcome = next((item.outcome for item in reported if item.outcome != "success"), "success")
        fact: dict[str, object] = {
            "id": indexer.get("id"), "name": indexer.get("name"), "status": outcome,
            "attempted": any(item.attempted for item in reported) if reported else True,
            "retry_after": max(60.0, site_request_cooldown(indexer)),
        }
        return torrents, fact

    def _refresh_site_cache(self, domain: str, stype: str) -> tuple[Dict[str, List[Context]], Dict[str, List[Context]]]:
        """
        在调用方持有短缓存锁时读取最新快照，仅清理本次站点的无效资源。
        """
        video_file, music_file = self._cache_file_pair(stype)
        video, music = self.load_cache(video_file) or {}, self.load_cache(music_file) or {}
        for cache in (video, music):
            self._ensure_context_compatibility(cache, stype=stype)
            cache[domain] = [context for context in cache.get(domain, []) if context.torrent_info
                             and not TorrentHelper().is_invalid(context.torrent_info.enclosure)]
        return video, music

    def _select_refresh_resources(self, torrents: List[TorrentInfo]) -> tuple[List[TorrentInfo], List[TorrentInfo]]:
        """
        保持影视与音乐各自的处理配额，并优先处理新发布资源。
        """
        torrents.sort(key=lambda item: item.pubdate or "", reverse=True)
        video = [item for item in torrents if item.category != MediaType.MUSIC.value]
        music = [item for item in torrents if item.category == MediaType.MUSIC.value]
        return video[:self.runtime_config.refresh_batch_size], music[:self.runtime_config.refresh_batch_size]

    def _commit_refresh_resources(
            self, domain: str, stype: str, resources: tuple[List[TorrentInfo], List[TorrentInfo]],
            prepared: dict[tuple[object, ...], Context],
    ) -> List[Context]:
        """
        重新读取并合并单站快照，保留并行站点写入及期间人工修正的识别结果。
        """
        with cache_mutation("torrent-snapshots"):
            caches = self._refresh_site_cache(domain, stype)
            for cache, torrents in zip(caches, resources):
                if self._is_no_cache_site(domain) and any(resources):
                    cache[domain] = []
                for torrent in self._refresh_cached_torrents(torrents, cache[domain]):
                    context = prepared.get(self._torrent_cache_key(torrent))
                    if context is not None:
                        cache[domain].append(context)
                cache[domain] = cache[domain][-self.runtime_config.torrent_cache_size:]
            for cache, filename in zip(caches, self._cache_file_pair(stype)):
                self.save_cache(cache, filename, strict=True)
            return list(caches[0][domain]) + list(caches[1][domain])

    def refresh_site(
            self, indexer: dict[str, Any], stype: str = "spider", include_music: bool = False,
    ) -> tuple[str, List[Context], dict[str, object]]:
        """
        独占一个站点直至抓取、识别和缓存合并真实结束，其他站点可同时处理。

        网络和媒体识别不持有共享缓存锁；只在读取与合并时短暂持锁。

        :param indexer: 已选择的站点索引配置，不写入排程快照或日志
        :param stype: 缓存与抓取模式，spider 或 rss
        :param include_music: 是否追加音乐专用入口
        :return: 站点域名、该站最新共享候选和不含凭据的执行事实
        """
        site_id = int(indexer["id"])
        domain = site_domain(indexer.get("domain"))
        with self._refresh_sites_lock:
            busy = site_id in self._refreshing_sites
            if not busy:
                self._refreshing_sites.add(site_id)
        if busy:
            with cache_mutation("torrent-snapshots"):
                caches = self._refresh_site_cache(domain, stype)
            return domain, list(caches[0][domain]) + list(caches[1][domain]), {
                "id": site_id, "name": indexer.get("name"),
                "status": "deferred", "attempted": False, "retry_after": 60,
            }
        started = time.monotonic()
        try:
            torrents, fact = self._fetch_refresh_resources(indexer, stype, include_music)
            resources = self._select_refresh_resources(torrents)
            with cache_mutation("torrent-snapshots"):
                caches = self._refresh_site_cache(domain, stype)
            prepared: dict[tuple[object, ...], Context] = {}
            for cache, items in zip(caches, resources):
                cached = [] if self._is_no_cache_site(domain) else cache[domain]
                for torrent in self._refresh_cached_torrents(items, cached):
                    if runtime_stop_state.is_system_stopped:
                        break
                    prepared[self._torrent_cache_key(torrent)] = self._build_refresh_context(torrent, stype)
            contexts = self._commit_refresh_resources(domain, stype, resources, prepared)
            fact.update(candidates=len(contexts), new_candidates=len(prepared), seconds=time.monotonic() - started)
            return domain, contexts, fact
        finally:
            with self._refresh_sites_lock:
                self._refreshing_sites.remove(site_id)

    @staticmethod
    def _torrent_cache_key(torrent: TorrentInfo) -> tuple[object, ...]:
        """
        优先使用站点和种子 ID；缺少稳定身份的旧资源沿用标题、副标题去重。
        """
        identity = torrent_resource_identity(torrent)
        if identity:
            return ("resource", *identity)
        return "title", torrent.site, torrent.title, torrent.description

    @classmethod
    def _refresh_cached_torrents(cls, torrents: List[TorrentInfo], cached: List[Context]) -> List[TorrentInfo]:
        """
        刷新已有资源信息，保留未变标题的识别结果；标题变化时替换旧候选。
        """
        contexts: dict[tuple[object, ...], Context] = {}
        for item in cached:
            if item.torrent_info is not None:
                contexts[cls._torrent_cache_key(item.torrent_info)] = item
        new_torrents: dict[tuple[object, ...], TorrentInfo] = {}
        for torrent in torrents:
            if not torrent.enclosure:
                continue
            key = cls._torrent_cache_key(torrent)
            context = contexts.get(key)
            if context is None:
                new_torrents[key] = torrent
                continue
            previous = context.torrent_info
            if previous is None:
                new_torrents[key] = torrent
                continue
            if (previous.title, previous.description) == (torrent.title, torrent.description):
                context.torrent_info = torrent
            else:
                cached.remove(context)
                contexts.pop(key)
                new_torrents[key] = torrent
        return list(new_torrents.values())

    def _is_no_cache_site(self, domain: str) -> bool:
        """
        判断站点是否配置为不缓存资源。
        """
        keys = (key.strip() for key in self.runtime_config.no_cache_site_key.split(","))
        return any(key and key in domain for key in keys)

    def _build_refresh_context(self, torrent: TorrentInfo, stype: str) -> Context:
        """
        识别单个种子并构造缓存上下文。
        """
        logger.info(f'处理资源：{torrent.title} ...')
        meta: MetaBase
        mediainfo: MediaInfo | MusicInfo
        if torrent.category == MediaType.MUSIC.value:
            meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description, mtype=MediaType.MUSIC)
            mediainfo = MusicInfo.from_meta(cast(MetaMusic, meta))
            candidate_recognized = False
            match_source = "unknown"
        else:
            video_meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description)
            if torrent.title != video_meta.org_string:
                logger.info(
                    f'种子名称应用识别词后发生改变：{torrent.title} => {video_meta.org_string}'
                )
            if video_meta.type != MediaType.TV and torrent.category == MediaType.TV.value:
                video_meta.type = MediaType.TV
            video_mediainfo = (
                MediaChain().recognize_by_meta(video_meta, obtain_images=False) or MediaInfo()
            )
            video_mediainfo.clear()
            candidate_recognized = bool(
                video_mediainfo and all(resolve_media_identity(media=video_mediainfo))
            )
            match_source = self._get_media_id_match_source(video_mediainfo)
            meta = video_meta
            mediainfo = video_mediainfo
        context = Context(
            meta_info=meta,
            media_info=mediainfo,
            torrent_info=torrent,
            resource_source="spider" if stype == "spider" else "rss",
            match_source=match_source if candidate_recognized else "unknown",
            candidate_recognized=candidate_recognized,
            media_info_is_target=False,
        )
        if not mediainfo or not all(resolve_media_identity(media=mediainfo)):
            context.media_recognize_fail_count = 1
        return context

    def refresh(
            self,
            stype: Optional[str] = None,
            sites: Optional[List[int]] = None,
            progress_callback: Optional[Callable[..., None]] = None,
            include_music: bool = False,
            site_observer: Optional[Callable[[dict[str, object]], None]] = None,
    ) -> Dict[str, List[Context]]:
        """
        刷新所选站点并合并共享候选，保留手工及插件入口的同步调用合同。

        :param stype: 强制指定 spider 或 rss，缺省使用当前订阅模式
        :param sites: 所选站点；空列表沿用默认订阅站点或全部站点的兼容语义
        :param progress_callback: 每个站点完成后更新进度
        :param include_music: 是否追加音乐专用入口
        :param site_observer: 接收各站点的请求、结果和候选事实
        :return: 本次所选站点的最新共享候选
        """
        stype = self._cache_type(stype)
        indexers = self.refresh_indexers(sites)
        candidates: Dict[str, List[Context]] = {}
        for index, indexer in enumerate(indexers, start=1):
            if runtime_stop_state.is_system_stopped:
                break
            if progress_callback:
                progress_callback(value=(index - 1) / len(indexers) * 100,
                                  text=f"正在刷新站点资源（{index}/{len(indexers)}）{indexer.get('name')} ...",
                                  data={"total": len(indexers), "finished": index - 1, "current": indexer.get("id")})
            domain, contexts, fact = self.refresh_site(indexer, stype, include_music)
            candidates[domain] = contexts
            if site_observer:
                site_observer(fact)
        if progress_callback:
            progress_callback(value=100, text="站点资源刷新完成",
                              data={"total": len(indexers), "finished": len(candidates)})
        return candidates

    @staticmethod
    def refresh_indexers(sites: Optional[List[int]]) -> list[dict[str, Any]]:
        """
        解析刷新入口共享的有效站点范围，空列表保留默认站点兼容语义。
        """
        if not sites:
            sites = get_configured_system_config().get(SystemConfigKey.RssSites) or []
        return [indexer for indexer in SitesHelper().get_indexers() if not sites or indexer.get("id") in sites]

    @staticmethod
    def _ensure_context_compatibility(
        torrents_cache: Dict[str, List[Context]], stype: Optional[str] = None
    ) -> None:
        """
        确保Context对象的兼容性，为旧版本添加缺失的字段
        """
        for domain, contexts in torrents_cache.items():
            for context in contexts:
                context_fields = vars(context)
                # 旧 pickle 实例会读到 dataclass 类默认值，必须检查实例字段，避免跳过兼容回填。
                if "media_recognize_fail_count" not in context_fields:
                    context.media_recognize_fail_count = 0
                    # 如果媒体信息未识别，设置初始失败次数
                    if not context.media_info or not all(
                            resolve_media_identity(media=context.media_info)
                    ):
                        context.media_recognize_fail_count = 1
                if "resource_source" not in context_fields:
                    context.resource_source = "spider" if stype == "spider" else "rss"
                if "candidate_recognized" not in context_fields:
                    context.candidate_recognized = bool(
                        context.media_info
                        and all(resolve_media_identity(media=context.media_info))
                    )
                if "match_source" not in context_fields:
                    context.match_source = (
                        TorrentsChain._get_media_id_match_source(context.media_info)
                        if context.candidate_recognized else "unknown"
                    )
                if "media_info_is_target" not in context_fields:
                    context.media_info_is_target = False

    @staticmethod
    def _get_media_id_match_source(
        mediainfo: Optional[Union[MediaInfo, MusicInfo]]
    ) -> str:
        """
        返回候选自身识别命中的媒体来源。
        """
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if media_source and media_id:
            return str(media_source)
        return "unknown"

    def __renew_rss_url(self, domain: str, site: dict[str, Any]) -> None:
        """
        保留原配置生成新的rss地址
        """
        try:
            # RSS链接过期
            logger.error(f"站点 {domain} RSS链接已过期，正在尝试自动获取！")
            # 自动生成rss地址
            site_url = site.get("url")
            site_cookie = site.get("cookie")
            site_rss = site.get("rss")
            if not isinstance(site_url, str) or not isinstance(site_cookie, str) or not isinstance(site_rss, str):
                return
            rss_url, errmsg = RssHelper().get_rss_link(
                url=site_url,
                cookie=site_cookie,
                ua=site.get("ua") or self.runtime_config.user_agent,
                proxy=True if site.get("proxy") else False,
                timeout=site.get("timeout"),
            )
            if rss_url:
                # 获取新的日期的passkey
                match = re.search(r'passkey=([a-zA-Z0-9]+)', rss_url)
                if match:
                    new_passkey = match.group(1)
                    # 获取过期rss除去passkey部分
                    new_rss = re.sub(r'&passkey=([a-zA-Z0-9]+)', f'&passkey={new_passkey}', site_rss)
                    logger.info(f"更新站点 {domain} RSS地址 ...")
                    self.site_repository.update_rss(domain=domain, rss=new_rss)
                else:
                    self.post_message(
                        Message(mtype=MessageType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                                link=self.runtime_config.site_url)
                    )
            else:
                self.post_message(
                    Message(mtype=MessageType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                            link=self.runtime_config.site_url))
        except Exception as e:
            logger.error(f"站点 {domain} RSS链接自动获取失败：{str(e)} - {traceback.format_exc()}")
            self.post_message(Message(mtype=MessageType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                                      link=self.runtime_config.site_url))
