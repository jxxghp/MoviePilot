import copy
import re
import traceback
from typing import Callable, Dict, List, Union, Optional

from app.helper.sites import SitesHelper  # noqa

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.core.config import settings, global_vars
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.rss import RssHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import Notification
from app.schemas.types import SystemConfigKey, MessageChannel, NotificationType, MediaType
from app.utils.string import StringUtils


class TorrentsChain(ChainBase):
    """
    站点首页或RSS种子处理链，服务于订阅、刷流等
    """

    _spider_file = "__torrents_cache__"
    _rss_file = "__rss_cache__"

    @property
    def cache_file(self) -> str:
        """
        返回缓存文件列表
        """
        if settings.SUBSCRIBE_MODE == 'spider':
            return self._spider_file
        return self._rss_file

    def remote_refresh(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程刷新订阅，发送消息
        """
        self.post_message(Notification(
            channel=channel,
            title=f"开始刷新种子 ...",
            userid=userid,
            save_history=False))
        self.refresh()
        self.post_message(Notification(
            channel=channel,
            title=f"种子刷新完成！",
            userid=userid,
            save_history=False))

    def get_torrents(self, stype: Optional[str] = None) -> Dict[str, List[Context]]:
        """
        获取当前缓存的种子
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """

        if not stype:
            stype = settings.SUBSCRIBE_MODE

        # 读取缓存
        if stype == 'spider':
            torrents_cache = self.load_cache(self._spider_file) or {}
        else:
            torrents_cache = self.load_cache(self._rss_file) or {}

        # 兼容性处理：为旧版本的Context对象补齐新增候选识别字段
        self._ensure_context_compatibility(torrents_cache, stype=stype)

        return torrents_cache

    async def async_get_torrents(self, stype: Optional[str] = None) -> Dict[str, List[Context]]:
        """
        异步获取当前缓存的种子
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """

        if not stype:
            stype = settings.SUBSCRIBE_MODE

        # 异步读取缓存
        if stype == 'spider':
            torrents_cache = await self.async_load_cache(self._spider_file) or {}
        else:
            torrents_cache = await self.async_load_cache(self._rss_file) or {}

        # 兼容性处理：为旧版本的Context对象补齐新增候选识别字段
        self._ensure_context_compatibility(torrents_cache, stype=stype)

        return torrents_cache

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
        results: List[Context] = []
        for contexts in (self.get_torrents(stype=stype) or {}).values():
            for context in contexts or []:
                if not context:
                    continue
                copied = copy.deepcopy(context)
                if self._context_matches_subscribe(copied, subscribe):
                    results.append(copied)
                    continue
                if allow_title_match and self._context_title_matches_subscribe(copied, subscribe):
                    self._mark_title_match_candidate(copied, subscribe)
                    results.append(copied)
        return results

    @classmethod
    def _context_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        严格身份匹配：候选自身识别出的媒体 ID 命中订阅，且季信息不排除订阅季。
        """
        if not cls._context_media_type_matches(context, subscribe):
            return False
        if not cls._context_season_matches_subscribe(context, subscribe):
            return False

        subscribe_tmdbid = cls._normalize_id(getattr(subscribe, "tmdbid", None))
        subscribe_doubanid = cls._normalize_id(getattr(subscribe, "doubanid", None))
        context_tmdbids = cls._context_tmdb_ids(context)
        context_doubanids = cls._context_douban_ids(context)

        return bool(
            subscribe_tmdbid and subscribe_tmdbid in context_tmdbids
            or subscribe_doubanid and subscribe_doubanid in context_doubanids
        )

    @classmethod
    def _context_title_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        标题兜底只服务诊断：仅允许身份缺失候选按标题命中，显式冲突 ID 不兜底。
        """
        if cls._context_has_media_identity(context):
            return False
        if not cls._context_media_type_matches(context, subscribe):
            return False
        if not cls._context_season_matches_subscribe(context, subscribe):
            return False

        subscribe_title = cls._normalize_title(getattr(subscribe, "name", None))
        if not subscribe_title:
            return False

        meta_info = getattr(context, "meta_info", None)
        torrent_info = getattr(context, "torrent_info", None)
        candidate_titles = [
            getattr(torrent_info, "title", None),
            getattr(meta_info, "title", None),
            getattr(meta_info, "name", None),
        ]
        return any(
            subscribe_title in candidate_title
            for candidate_title in (cls._normalize_title(title) for title in candidate_titles)
            if candidate_title
        )

    @staticmethod
    def _mark_title_match_candidate(context: Context, subscribe) -> None:
        """
        标记标题兜底候选，避免下游把目标媒体回填误认为候选自身识别结果。
        """
        context.match_source = "title"
        context.candidate_recognized = False
        context.media_info_is_target = True
        context.media_info = MediaInfo(
            type=getattr(subscribe, "type", None),
            title=getattr(subscribe, "name", None),
            tmdb_id=getattr(subscribe, "tmdbid", None),
            douban_id=getattr(subscribe, "doubanid", None),
            season=getattr(subscribe, "season", None),
        )

    @classmethod
    def _context_media_type_matches(cls, context: Context, subscribe) -> bool:
        """
        类型已知且冲突时拒绝；缺失类型不作为缓存候选过滤条件。
        """
        subscribe_type = cls._normalize_media_type(getattr(subscribe, "type", None))
        media_info = getattr(context, "media_info", None)
        meta_info = getattr(context, "meta_info", None)
        context_types = {
            cls._normalize_media_type(value)
            for value in (
                getattr(media_info, "type", None),
                getattr(meta_info, "type", None),
            )
        }
        context_types.discard(None)
        return not subscribe_type or not context_types or all(
            context_type == subscribe_type for context_type in context_types
        )

    @classmethod
    def _context_season_matches_subscribe(cls, context: Context, subscribe) -> bool:
        """
        资源季信息只要明确排除订阅季就拒绝；跨季覆盖目标季留给插件诊断。
        """
        target_season = cls._normalize_int(getattr(subscribe, "season", None))
        if target_season is None:
            return True

        meta_info = getattr(context, "meta_info", None)
        explicit_meta_seasons = cls._context_meta_seasons(meta_info)
        if explicit_meta_seasons:
            return target_season in explicit_meta_seasons

        media_info = getattr(context, "media_info", None)
        media_season = cls._normalize_int(getattr(media_info, "season", None))
        return media_season is None or target_season == media_season

    @classmethod
    def _context_meta_seasons(cls, meta_info) -> set[int]:
        """
        提取标题解析出的显式季范围；多季包以该范围为准。
        """
        meta_fields = vars(meta_info) if meta_info else {}
        if "season_list" in meta_fields:
            season_list = {
                season
                for season in (
                    cls._normalize_int(item)
                    for item in (meta_fields.get("season_list") or [])
                )
                if season is not None
            }
            if season_list:
                return season_list
        begin_season = cls._normalize_int(getattr(meta_info, "begin_season", None))
        end_season = cls._normalize_int(getattr(meta_info, "end_season", None))
        if begin_season is not None and end_season is not None:
            start, end = sorted((begin_season, end_season))
            return set(range(start, end + 1))
        if begin_season is not None:
            return {begin_season}
        if end_season is not None:
            return {end_season}
        return set()

    @staticmethod
    def _context_has_media_identity(context: Context) -> bool:
        """
        判断候选是否已经带有明确媒体 ID。
        """
        return bool(TorrentsChain._context_tmdb_ids(context) or TorrentsChain._context_douban_ids(context))

    @staticmethod
    def _context_tmdb_ids(context: Context) -> set[str]:
        """
        提取候选已有 TMDB ID，兼容 media_info 与标题显式标签。
        """
        media_info = getattr(context, "media_info", None)
        meta_info = getattr(context, "meta_info", None)
        return {
            value for value in (
                TorrentsChain._normalize_id(getattr(media_info, "tmdb_id", None)),
                TorrentsChain._normalize_id(getattr(meta_info, "tmdbid", None)),
            ) if value
        }

    @staticmethod
    def _context_douban_ids(context: Context) -> set[str]:
        """
        提取候选已有豆瓣 ID，兼容 media_info 与标题显式标签。
        """
        media_info = getattr(context, "media_info", None)
        meta_info = getattr(context, "meta_info", None)
        return {
            value for value in (
                TorrentsChain._normalize_id(getattr(media_info, "douban_id", None)),
                TorrentsChain._normalize_id(getattr(meta_info, "doubanid", None)),
            ) if value
        }

    @staticmethod
    def _normalize_id(value) -> Optional[str]:
        """
        统一比较媒体 ID，避免 int/string 形态差异影响缓存候选筛选。
        """
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @staticmethod
    def _normalize_int(value) -> Optional[int]:
        """
        将季号等动态字段转为 int，无法解析时视为缺失。
        """
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_media_type(value) -> Optional[str]:
        """
        统一 MediaType 枚举与字符串形态。
        """
        if isinstance(value, MediaType):
            value = value.value
        if value == MediaType.UNKNOWN.value:
            return None
        return value

    @staticmethod
    def _normalize_title(value) -> str:
        """
        归一标题用于低置信标题兜底匹配。
        """
        return (StringUtils.clear_upper(value or "") or "").strip()

    def clear_torrents(self):
        """
        清理种子缓存数据
        """
        logger.info(f'开始清理种子缓存数据 ...')
        self.remove_cache(self._spider_file)
        self.remove_cache(self._rss_file)
        logger.info(f'种子缓存数据清理完成')

    async def async_clear_torrents(self):
        """
        异步清理种子缓存数据
        """
        logger.info(f'开始异步清理种子缓存数据 ...')
        await self.async_remove_cache(self._spider_file)
        await self.async_remove_cache(self._rss_file)
        logger.info(f'异步种子缓存数据清理完成')

    def browse(self, domain: str, keyword: Optional[str] = None, cat: Optional[str] = None,
               page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        浏览站点首页内容，返回种子清单，TTL缓存5分钟
        :param domain: 站点域名
        :param keyword: 搜索标题
        :param cat: 搜索分类
        :param page: 页码
        """
        logger.info(f'开始获取站点 {domain} 最新种子 ...')
        site = SitesHelper().get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return self.refresh_torrents(site=site, keyword=keyword, cat=cat, page=page)

    async def async_browse(self, domain: str, keyword: Optional[str] = None, cat: Optional[str] = None,
                           page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        异步浏览站点首页内容，返回种子清单，TTL缓存5分钟
        :param domain: 站点域名
        :param keyword: 搜索标题
        :param cat: 搜索分类
        :param page: 页码
        """
        logger.info(f'开始获取站点 {domain} 最新种子 ...')
        site = await SitesHelper().async_get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return await self.async_refresh_torrents(site=site, keyword=keyword, cat=cat, page=page)

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
                    site_ua=site.get("ua") or settings.USER_AGENT,
                    site_proxy=site.get("proxy"),
                    site_order=site.get("pri"),
                    site_downloader=site.get("downloader"),
                    title=item.get("title"),
                    enclosure=item.get("enclosure"),
                    page_url=item.get("link"),
                    size=item.get("size"),
                    pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
                )
                ret_torrents.append(torrentinfo)
        finally:
            rss_items.clear()
            del rss_items
        return ret_torrents

    def refresh(
            self,
            stype: Optional[str] = None,
            sites: List[int] = None,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, List[Context]]:
        """
        刷新站点最新资源，识别并缓存起来
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        :param sites: 强制指定站点ID列表，为空则读取设置的订阅站点
        :param progress_callback: 资源刷新进度更新回调
        """

        def __is_no_cache_site(_domain: str) -> bool:
            """
            判断站点是否不需要缓存
            """
            for url_key in settings.NO_CACHE_SITE_KEY.split(','):
                if url_key in _domain:
                    return True
            return False

        # 刷新类型
        if not stype:
            stype = settings.SUBSCRIBE_MODE

        # 刷新站点
        if not sites:
            sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []

        # 读取缓存
        torrents_cache = self.get_torrents()

        # 缓存过滤掉无效种子
        for _domain, _torrents in torrents_cache.items():
            torrents_cache[_domain] = [_torrent for _torrent in _torrents
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
            if global_vars.is_system_stopped:
                break
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_indexers * 100 if total_indexers else 100,
                    text=(
                        f"正在刷新站点资源（{index}/{total_indexers}）"
                        f"{indexer.get('name')} ..."
                    ),
                    data={
                        "total": total_indexers,
                        "finished": index - 1,
                        "current": indexer.get("id"),
                    },
                )
            domain = StringUtils.get_url_domain(indexer.get("domain"))
            domains.append(domain)
            if stype == "spider":
                # 刷新首页种子
                torrents: List[TorrentInfo] = []
                # 读取第0页和第1页
                for page in range(2):
                    page_torrents = self.browse(domain=domain, page=page)
                    if page_torrents:
                        torrents.extend(page_torrents)
                    else:
                        # 如果某一页没有数据，说明已经到最后一页，停止获取
                        break
            else:
                # 刷新RSS种子
                torrents: List[TorrentInfo] = self.rss(domain=domain)
            # 按pubdate降序排列
            torrents.sort(key=lambda x: x.pubdate or '', reverse=True)
            # 取前N条
            torrents = torrents[:settings.CONF.refresh]
            if torrents:
                if __is_no_cache_site(domain):
                    # 不需要缓存的站点，直接处理
                    logger.info(f'{indexer.get("name")} 有 {len(torrents)} 个种子 (不缓存)')
                    torrents_cache[domain] = []
                else:
                    # 过滤出没有处理过的种子 - 优化：使用集合查找，避免重复创建字符串列表
                    cached_signatures = {f'{t.torrent_info.title}{t.torrent_info.description}'
                                         for t in torrents_cache.get(domain) or []}
                    torrents = [torrent for torrent in torrents
                                if f'{torrent.title}{torrent.description}' not in cached_signatures]
                if torrents:
                    logger.info(f'{indexer.get("name")} 有 {len(torrents)} 个新种子')
                else:
                    logger.info(f'{indexer.get("name")} 没有新种子')
                    continue
                try:
                    for torrent in torrents:
                        if global_vars.is_system_stopped:
                            break
                        if not torrent.enclosure:
                            logger.warn(f"缺少种子链接，忽略处理: {torrent.title}")
                            continue
                        logger.info(f'处理资源：{torrent.title} ...')
                        # 识别
                        meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
                        if torrent.title != meta.org_string:
                            logger.info(f'种子名称应用识别词后发生改变：{torrent.title} => {meta.org_string}')
                        # 使用站点种子分类，校正类型识别
                        if meta.type != MediaType.TV \
                                and torrent.category == MediaType.TV.value:
                            meta.type = MediaType.TV
                        # 识别媒体信息
                        mediainfo: MediaInfo = MediaChain().recognize_by_meta(
                            meta,
                            obtain_images=False,
                        )
                        if not mediainfo:
                            logger.warn(f'{torrent.title} 未识别到媒体信息')
                            # 存储空的媒体信息
                            mediainfo = MediaInfo()
                        # 清理多余数据，减少内存占用
                        mediainfo.clear()
                        candidate_recognized = bool(mediainfo and (mediainfo.tmdb_id or mediainfo.douban_id))
                        match_source = self._get_media_id_match_source(mediainfo)
                        # 上下文
                        context = Context(
                            meta_info=meta,
                            media_info=mediainfo,
                            torrent_info=torrent,
                            resource_source="spider" if stype == "spider" else "rss",
                            match_source=match_source if candidate_recognized else "unknown",
                            candidate_recognized=candidate_recognized,
                            media_info_is_target=False,
                        )
                        # 如果未识别到媒体信息，设置初始失败次数为1
                        if not mediainfo or (not mediainfo.tmdb_id and not mediainfo.douban_id):
                            context.media_recognize_fail_count = 1
                        # 添加到缓存
                        if not torrents_cache.get(domain):
                            torrents_cache[domain] = [context]
                        else:
                            torrents_cache[domain].append(context)
                        # 如果超过了限制条数则移除掉前面的
                        if len(torrents_cache[domain]) > settings.CONF.torrents:
                            torrents_cache[domain] = torrents_cache[domain][-settings.CONF.torrents:]
                finally:
                    torrents.clear()
                    del torrents
            else:
                logger.info(f'{indexer.get("name")} 没有获取到种子')

        # 保存缓存到本地
        if stype == "spider":
            self.save_cache(torrents_cache, self._spider_file)
        else:
            self.save_cache(torrents_cache, self._rss_file)

        # 去除不在站点范围内的缓存种子
        if sites and torrents_cache:
            torrents_cache = {k: v for k, v in torrents_cache.items() if k in domains}

        if progress_callback:
            progress_callback(
                value=100,
                text="站点资源刷新完成",
                data={"total": total_indexers, "finished": total_indexers},
            )

        return torrents_cache

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
                    if (not context.media_info or
                            (not context.media_info.tmdb_id and not context.media_info.douban_id)):
                        context.media_recognize_fail_count = 1
                if "resource_source" not in context_fields:
                    context.resource_source = "spider" if stype == "spider" else "rss"
                if "candidate_recognized" not in context_fields:
                    context.candidate_recognized = bool(
                        context.media_info and (context.media_info.tmdb_id or context.media_info.douban_id)
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
        返回候选自身识别命中的明确媒体 ID 类型。
        """
        if mediainfo and mediainfo.tmdb_id:
            return "tmdbid"
        if mediainfo and mediainfo.douban_id:
            return "doubanid"
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
                ua=site.get("ua") or settings.USER_AGENT,
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
                    SiteOper().update_rss(domain=domain, rss=new_rss)
                else:
                    # 发送消息
                    self.post_message(
                        Notification(mtype=NotificationType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                                     link=settings.MP_DOMAIN('#/site'))
                    )
            else:
                self.post_message(
                    Notification(mtype=NotificationType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                                 link=settings.MP_DOMAIN('#/site')))
        except Exception as e:
            logger.error(f"站点 {domain} RSS链接自动获取失败：{str(e)} - {traceback.format_exc()}")
            self.post_message(Notification(mtype=NotificationType.SiteMessage, title=f"站点 {domain} RSS链接已过期",
                                           link=settings.MP_DOMAIN('#/site')))
