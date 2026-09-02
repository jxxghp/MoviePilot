import re
import traceback
from typing import Callable, Dict, List, Optional, Union

from app.application.configuration import get_configured_system_config
from app.application.rss import RssHelper
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.subscription.candidates import CandidateIndex
from app.application.torrent.download import TorrentHelper
from app.chain.base import ChainBase
from app.chain.media import MediaChain
from app.domain import site as site_rules
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import resolve_media_identity
from app.schemas.message import Message
from app.schemas.types import MediaType, MessageType, NotificationChannel, SystemConfigKey


class TorrentsChain(ChainBase):
    """
    站点首页或RSS种子处理链，服务于订阅、刷流等
    """

    _spider_file = "__torrents_cache__"
    _rss_file = "__rss_cache__"
    # 音乐资源独立缓存，与影视种子分开计算配额与存储，避免被影视资源挤出
    _music_spider_file = "__torrents_music_cache__"
    _music_rss_file = "__rss_music_cache__"

    def _cache_type(self, stype: Optional[str]) -> str:
        """把可选缓存类型统一为本次读取使用的明确订阅模式。"""
        return stype or self.runtime_config.subscribe_mode

    def _cache_file_pair(self, stype: str) -> tuple[str, str]:
        """返回指定订阅模式下影视与音乐缓存的唯一文件映射。"""
        if stype == 'spider':
            return self._spider_file, self._music_spider_file
        return self._rss_file, self._music_rss_file

    @staticmethod
    def _merge_torrent_caches(
        torrents_cache: Dict[str, List[Context]],
        music_cache: Dict[str, List[Context]],
    ) -> Dict[str, List[Context]]:
        """按站点稳定合并音乐独立缓存，空列表不创建无效站点键。"""
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

    def remote_refresh(self, channel: NotificationChannel, userid: Union[str, int] = None):
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

    def cache_files(self, stype: Optional[str] = None) -> tuple:
        """
        返回影视与音乐缓存文件名，供按当前订阅模式回写各自缓存
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """
        return self._cache_file_pair(self._cache_type(stype))

    @staticmethod
    def split_cache_contexts(
            torrents_cache: Dict[str, List[Context]],
    ) -> tuple:
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
            subscribe,
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
    def _context_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        严格身份匹配：候选自身识别出的媒体 ID 命中订阅，且季信息不排除订阅季。
        """
        return CandidateIndex.strict_matches(context, subscribe)

    @classmethod
    def _context_title_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        标题兜底只服务诊断：仅允许身份缺失候选按标题命中，显式冲突 ID 不兜底。
        """
        return CandidateIndex.title_matches(context, subscribe)

    @staticmethod
    def _mark_title_match_candidate(context: Context, subscribe) -> None:
        """
        标记标题兜底候选，避免下游把目标媒体回填误认为候选自身识别结果。
        """
        CandidateIndex.mark_title_candidate(context, subscribe)

    @classmethod
    def _context_media_type_matches(cls, context: Context, subscribe) -> bool:
        """
        类型已知且冲突时拒绝；缺失类型不作为缓存候选过滤条件。
        """
        return CandidateIndex.media_type_matches(context, subscribe)

    @classmethod
    def _context_season_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        资源季信息只要明确排除订阅季就拒绝；跨季覆盖目标季留给插件诊断。
        """
        return CandidateIndex.season_matches(context, subscribe)

    @classmethod
    def _context_meta_seasons(cls, meta_info) -> set[int]:
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
        """提取候选媒体信息与标题标签中的通用媒体身份。"""
        return CandidateIndex.context_identities(context)

    @staticmethod
    def _normalize_int(value) -> Optional[int]:
        """
        将季号等动态字段转为 int，无法解析时视为缺失。
        """
        return CandidateIndex.normalize_int(value)

    @staticmethod
    def _normalize_media_type(value) -> Optional[str]:
        """
        统一 MediaType 枚举与字符串形态。
        """
        return CandidateIndex.normalize_media_type(value)

    @staticmethod
    def _normalize_title(value) -> str:
        """
        归一标题用于低置信标题兜底匹配。
        """
        return CandidateIndex.normalize_title(value)

    def clear_torrents(self):
        """
        清理种子缓存数据，包含音乐独立缓存
        """
        logger.info('开始清理种子缓存数据 ...')
        self.remove_cache(self._spider_file)
        self.remove_cache(self._rss_file)
        self.remove_cache(self._music_spider_file)
        self.remove_cache(self._music_rss_file)
        logger.info('种子缓存数据清理完成')

    async def async_clear_torrents(self):
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
        if not site.get("rss"):
            logger.error(f'站点 {domain} 未配置RSS地址！')
            return []
        # 解析RSS
        rss_items = RssHelper().parse(site.get("rss"), True if site.get("proxy") else False,
                                      timeout=int(site.get("timeout") or 30),
                                      ua=site.get("ua") if site.get("ua") else None)
        if rss_items is None:
            # rss过期，尝试保留原配置生成新的rss
            self.__renew_rss_url(domain=domain, site=site)
            return []
        if not rss_items:
            logger.error(f'站点 {domain} 未获取到RSS数据！')
            return []
        # 站点级媒体类型，用于给缺少分类信息的 RSS 种子补充分类
        site_media_type = MediaType.from_agent(site.get("media_type"))
        # 组装种子
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
                    site_proxy=site.get("proxy"),
                    site_order=site.get("pri"),
                    site_downloader=site.get("downloader"),
                    title=item.get("title"),
                    enclosure=item.get("enclosure"),
                    page_url=item.get("link"),
                    size=item.get("size"),
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
    def _music_browse_paths(site: dict) -> List[str]:
        """
        返回站点独立于默认浏览入口的音乐种子页面路径。

        部分站点的默认种子列表只显示电影和电视剧，音乐需要单独的菜单页面进入；
        这类站点在索引配置中用 type=music 的搜索路径声明音乐入口。
        默认入口已覆盖音乐（音乐站点或未定义独立入口）时返回空列表。
        """
        # 音乐站点全站都是音乐资源，默认浏览入口已经覆盖
        if MediaType.from_agent(site.get("media_type")) == MediaType.MUSIC:
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
    ) -> List[TorrentInfo]:
        """
        追加抓取站点音乐专用入口的最新种子，并按种子链接去重后返回合并结果。
        """
        seen = {torrent.enclosure for torrent in torrents if torrent.enclosure}
        for page in range(2):
            page_torrents = self.browse(domain=domain, page=page, mtype=MediaType.MUSIC)
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

    def _refresh_indexer(
            self,
            indexer: dict,
            stype: str,
            include_music: bool,
            torrents_cache: Dict[str, List[Context]],
            music_cache: Dict[str, List[Context]],
    ) -> str:
        """抓取并写入单个站点的影视、音乐资源缓存。"""
        domain = site_rules.extract_domain(indexer.get("domain"))
        if stype == "spider":
            torrents: List[TorrentInfo] = []
            for page in range(2):
                page_torrents = self.browse(domain=domain, page=page)
                if not page_torrents:
                    break
                torrents.extend(page_torrents)
        else:
            torrents = self.rss(domain=domain)
        if include_music and self._music_browse_paths(indexer):
            torrents = self.__append_music_browse_torrents(domain=domain, torrents=torrents)
        torrents.sort(key=lambda item: item.pubdate or "", reverse=True)
        music_torrents = [
            item for item in torrents if item.category == MediaType.MUSIC.value
        ][:self.runtime_config.refresh_batch_size]
        torrents = [
            item for item in torrents if item.category != MediaType.MUSIC.value
        ][:self.runtime_config.refresh_batch_size]
        if not torrents and not music_torrents:
            logger.info(f'{indexer.get("name")} 没有获取到种子')
            return domain
        if self._is_no_cache_site(domain):
            logger.info(
                f'{indexer.get("name")} 有 {len(torrents) + len(music_torrents)} 个种子 (不缓存)'
            )
            torrents_cache[domain] = []
            music_cache[domain] = []
        else:
            cached_signatures = {
                f'{item.torrent_info.title}{item.torrent_info.description}'
                for item in torrents_cache.get(domain) or []
            }
            torrents = [
                item for item in torrents
                if f'{item.title}{item.description}' not in cached_signatures
            ]
            music_signatures = {
                f'{item.torrent_info.title}{item.torrent_info.description}'
                for item in music_cache.get(domain) or []
            }
            music_torrents = [
                item for item in music_torrents
                if f'{item.title}{item.description}' not in music_signatures
            ]
        if not torrents and not music_torrents:
            logger.info(f'{indexer.get("name")} 没有新种子')
            return domain
        logger.info(f'{indexer.get("name")} 有 {len(torrents) + len(music_torrents)} 个新种子')
        for torrent in torrents + music_torrents:
            if runtime_stop_state.is_system_stopped:
                break
            if not torrent.enclosure:
                logger.warning(f"缺少种子链接，忽略处理: {torrent.title}")
                continue
            context = self._build_refresh_context(torrent, stype)
            target_cache = music_cache if torrent.category == MediaType.MUSIC.value else torrents_cache
            target_cache.setdefault(domain, []).append(context)
            if len(target_cache[domain]) > self.runtime_config.torrent_cache_size:
                target_cache[domain] = target_cache[domain][-self.runtime_config.torrent_cache_size:]
        return domain

    def _is_no_cache_site(self, domain: str) -> bool:
        """判断站点是否配置为不缓存资源。"""
        return any(key in domain for key in self.runtime_config.no_cache_site_key.split(","))

    def _build_refresh_context(self, torrent: TorrentInfo, stype: str) -> Context:
        """识别单个种子并构造缓存上下文。"""
        logger.info(f'处理资源：{torrent.title} ...')
        meta: MetaBase
        mediainfo: MediaInfo | MusicInfo
        if torrent.category == MediaType.MUSIC.value:
            meta = MetaMusic.parse_query(torrent.title)
            mediainfo = MusicInfo(
                title=meta.title,
                artists=list(meta.artists),
                album=meta.album,
                year=meta.year,
                names=[meta.title] if meta.title else [],
            )
            candidate_recognized = False
            match_source = "unknown"
        else:
            video_meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
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
            sites: List[int] = None,
            progress_callback: Optional[Callable[..., None]] = None,
            include_music: bool = False,
    ) -> Dict[str, List[Context]]:
        """
        刷新站点最新资源并返回本轮可匹配的完整候选缓存。

        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        :param sites: 强制指定站点ID列表，为空则读取设置的订阅站点
        :param progress_callback: 资源刷新进度更新回调
        :param include_music: 是否额外抓取站点的音乐专用浏览入口，服务音乐订阅
        """
        # 刷新类型
        if not stype:
            stype = self.runtime_config.subscribe_mode

        # 刷新站点
        if not sites:
            sites = get_configured_system_config().get(SystemConfigKey.RssSites) or []

        # 读取缓存，影视与音乐分别独立存储
        if stype == 'spider':
            torrents_cache = self.load_cache(self._spider_file) or {}
            music_cache = self.load_cache(self._music_spider_file) or {}
        else:
            torrents_cache = self.load_cache(self._rss_file) or {}
            music_cache = self.load_cache(self._music_rss_file) or {}
        self._ensure_context_compatibility(torrents_cache, stype=stype)
        self._ensure_context_compatibility(music_cache, stype=stype)

        # 缓存过滤掉无效种子（影视与音乐缓存分别处理）
        for _cache in (torrents_cache, music_cache):
            for _domain, _torrents in _cache.items():
                _cache[_domain] = [_torrent for _torrent in _torrents
                                   if not TorrentHelper().is_invalid(_torrent.torrent_info.enclosure)]

        # 需要刷新的站点domain
        domains = []
        indexers = [
            indexer for indexer in SitesHelper().get_indexers()
            if not sites or indexer.get("id") in sites
        ]
        total_indexers = len(indexers)
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始刷新站点资源，共 {total_indexers} 个站点 ...",
                data={"total": total_indexers, "finished": 0},
            )
        # 遍历站点缓存资源
        for index, indexer in enumerate(indexers, start=1):
            if runtime_stop_state.is_system_stopped:
                break
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_indexers * 100 if total_indexers else 100,
                    text=f"正在刷新站点资源（{index}/{total_indexers}）{indexer.get('name')} ...",
                    data={
                        "total": total_indexers,
                        "finished": index - 1,
                        "current": indexer.get("id"),
                    },
                )
            domains.append(self._refresh_indexer(
                indexer=indexer,
                stype=stype,
                include_music=include_music,
                torrents_cache=torrents_cache,
                music_cache=music_cache,
            ))

        # 保存缓存到本地，影视与音乐分别存储
        if stype == "spider":
            self.save_cache(torrents_cache, self._spider_file)
            self.save_cache(music_cache, self._music_spider_file)
        else:
            self.save_cache(torrents_cache, self._rss_file)
            self.save_cache(music_cache, self._music_rss_file)

        # 去除不在站点范围内的缓存种子
        if sites and torrents_cache:
            torrents_cache = {k: v for k, v in torrents_cache.items() if k in domains}
        if sites and music_cache:
            music_cache = {k: v for k, v in music_cache.items() if k in domains}

        if progress_callback:
            progress_callback(
                value=100,
                text="站点资源刷新完成",
                data={"total": total_indexers, "finished": total_indexers},
            )

        return self._merge_torrent_caches(
            {domain: list(contexts) for domain, contexts in torrents_cache.items()},
            music_cache,
        )

    @staticmethod
    def _ensure_context_compatibility(torrents_cache: Dict[str, List[Context]], stype: Optional[str] = None):
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
    def _get_media_id_match_source(mediainfo: Optional[MediaInfo]) -> str:
        """
        返回候选自身识别命中的媒体来源。
        """
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if media_source and media_id:
            return str(media_source)
        return "unknown"

    def __renew_rss_url(self, domain: str, site: dict):
        """
        保留原配置生成新的rss地址
        """
        try:
            # RSS链接过期
            logger.error(f"站点 {domain} RSS链接已过期，正在尝试自动获取！")
            # 自动生成rss地址
            rss_url, errmsg = RssHelper().get_rss_link(
                url=site.get("url"),
                cookie=site.get("cookie"),
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
                    new_rss = re.sub(r'&passkey=([a-zA-Z0-9]+)', f'&passkey={new_passkey}', site.get("rss"))
                    logger.info(f"更新站点 {domain} RSS地址 ...")
                    self.site_repository.update_rss(domain=domain, rss=new_rss)
                else:
                    # 发送消息
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
