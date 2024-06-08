import re
import traceback
from typing import Dict, List, Union

from cachetools import cached, TTLCache

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.core.config import settings
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.rss import RssHelper
from app.helper.sites import SitesHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import Notification
from app.schemas.types import SystemConfigKey, MessageChannel, NotificationType, MediaType
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class TorrentsChain(ChainBase, metaclass=Singleton):
    """
    站点首页或RSS种子处理链，服务于订阅、刷流等
    """

    _spider_file = "__torrents_cache__"
    _rss_file = "__rss_cache__"

    def __init__(self):
        super().__init__()
        self.siteshelper = SitesHelper()
        self.siteoper = SiteOper()
        self.rsshelper = RssHelper()
        self.systemconfig = SystemConfigOper()
        self.mediachain = MediaChain()
        self.torrenthelper = TorrentHelper()

    def remote_refresh(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程刷新订阅，发送消息
        """
        self.post_message(Notification(channel=channel,
                                       title=f"开始刷新种子 ...", userid=userid))
        self.refresh()
        self.post_message(Notification(channel=channel,
                                       title=f"种子刷新完成！", userid=userid))

    def get_torrents(self, stype: str = None) -> Dict[str, List[Context]]:
        """
        获取当前缓存的种子
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        """

        if not stype:
            stype = settings.SUBSCRIBE_MODE

        # 读取缓存
        if stype == 'spider':
            return self.load_cache(self._spider_file) or {}
        else:
            return self.load_cache(self._rss_file) or {}

    def clear_torrents(self):
        """
        清理种子缓存数据
        """
        logger.info(f'开始清理种子缓存数据 ...')
        self.remove_cache(self._spider_file)
        self.remove_cache(self._rss_file)
        logger.info(f'种子缓存数据清理完成')

    @cached(cache=TTLCache(maxsize=128, ttl=595))
    def browse(self, domain: str) -> List[TorrentInfo]:
        """
        浏览站点首页内容，返回种子清单，TTL缓存10分钟
        :param domain: 站点域名
        """
        logger.info(f'开始获取站点 {domain} 最新种子 ...')
        site = self.siteshelper.get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return self.refresh_torrents(site=site)

    @cached(cache=TTLCache(maxsize=128, ttl=295))
    def rss(self, domain: str) -> List[TorrentInfo]:
        """
        获取站点RSS内容，返回种子清单，TTL缓存5分钟
        :param domain: 站点域名
        """
        logger.info(f'开始获取站点 {domain} RSS ...')
        site = self.siteshelper.get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        if not site.get("rss"):
            logger.error(f'站点 {domain} 未配置RSS地址！')
            return []
        rss_items = self.rsshelper.parse(site.get("rss"), True if site.get("proxy") else False,
                                         timeout=int(site.get("timeout") or 30))
        if rss_items is None:
            # rss过期，尝试保留原配置生成新的rss
            self.__renew_rss_url(domain=domain, site=site)
            return []
        if not rss_items:
            logger.error(f'站点 {domain} 未获取到RSS数据！')
            return []
        # 组装种子
        ret_torrents: List[TorrentInfo] = []
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
                title=item.get("title"),
                enclosure=item.get("enclosure"),
                page_url=item.get("link"),
                size=item.get("size"),
                pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
            )
            ret_torrents.append(torrentinfo)

        return ret_torrents

    def refresh(self, stype: str = None, sites: List[int] = None) -> Dict[str, List[Context]]:
        """
        刷新站点最新资源，识别并缓存起来
        :param stype: 强制指定缓存类型，spider:爬虫缓存，rss:rss缓存
        :param sites: 强制指定站点ID列表，为空则读取设置的订阅站点
        """
        # 刷新类型
        if not stype:
            stype = settings.SUBSCRIBE_MODE

        # 刷新站点
        if not sites:
            sites = self.systemconfig.get(SystemConfigKey.RssSites) or []

        # 读取缓存
        torrents_cache = self.get_torrents()

        # 缓存过滤掉无效种子
        for _domain, _torrents in torrents_cache.items():
            torrents_cache[_domain] = [_torrent for _torrent in _torrents
                                       if not self.torrenthelper.is_invalid(_torrent.torrent_info.enclosure)]

        # 所有站点索引
        indexers = self.siteshelper.get_indexers()
        # 需要刷新的站点domain
        domains = []
        # 遍历站点缓存资源
        for indexer in indexers:
            # 未开启的站点不刷新
            if sites and indexer.get("id") not in sites:
                continue
            domain = StringUtils.get_url_domain(indexer.get("domain"))
            domains.append(domain)
            if stype == "spider":
                # 刷新首页种子
                torrents: List[TorrentInfo] = self.browse(domain=domain)
            else:
                # 刷新RSS种子
                torrents: List[TorrentInfo] = self.rss(domain=domain)
            # 按pubdate降序排列
            torrents.sort(key=lambda x: x.pubdate or '', reverse=True)
            # 取前N条
            torrents = torrents[:settings.CACHE_CONF.get('refresh')]
            if torrents:
                # 过滤出没有处理过的种子
                torrents = [torrent for torrent in torrents
                            if f'{torrent.title}{torrent.description}'
                            not in [f'{t.torrent_info.title}{t.torrent_info.description}'
                                    for t in torrents_cache.get(domain) or []]]
                if torrents:
                    logger.info(f'{indexer.get("name")} 有 {len(torrents)} 个新种子')
                else:
                    logger.info(f'{indexer.get("name")} 没有新种子')
                    continue
                for torrent in torrents:
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
                    mediainfo: MediaInfo = self.mediachain.recognize_by_meta(meta)
                    if not mediainfo:
                        logger.warn(f'{torrent.title} 未识别到媒体信息')
                        # 存储空的媒体信息
                        mediainfo = MediaInfo()
                    # 清理多余数据
                    mediainfo.clear()
                    # 上下文
                    context = Context(meta_info=meta, media_info=mediainfo, torrent_info=torrent)
                    # 添加到缓存
                    if not torrents_cache.get(domain):
                        torrents_cache[domain] = [context]
                    else:
                        torrents_cache[domain].append(context)
                    # 如果超过了限制条数则移除掉前面的
                    if len(torrents_cache[domain]) > settings.CACHE_CONF.get('torrents'):
                        torrents_cache[domain] = torrents_cache[domain][-settings.CACHE_CONF.get('torrents'):]
                # 回收资源
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
        return torrents_cache

    def __renew_rss_url(self, domain: str, site: dict):
        """
        保留原配置生成新的rss地址
        """
        try:
            # RSS链接过期
            logger.error(f"站点 {domain} RSS链接已过期，正在尝试自动获取！")
            # 自动生成rss地址
            rss_url, errmsg = self.rsshelper.get_rss_link(
                url=site.get("url"),
                cookie=site.get("cookie"),
                ua=site.get("ua") or settings.USER_AGENT,
                proxy=True if site.get("proxy") else False
            )
            if rss_url:
                # 获取新的日期的passkey
                match = re.search(r'passkey=([a-zA-Z0-9]+)', rss_url)
                if match:
                    new_passkey = match.group(1)
                    # 获取过期rss除去passkey部分
                    new_rss = re.sub(r'&passkey=([a-zA-Z0-9]+)', f'&passkey={new_passkey}', site.get("rss"))
                    logger.info(f"更新站点 {domain} RSS地址 ...")
                    self.siteoper.update_rss(domain=domain, rss=new_rss)
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
