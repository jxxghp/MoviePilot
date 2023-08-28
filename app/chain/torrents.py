from typing import Dict, List, Union

from requests import Session

from app.chain import ChainBase
from app.core.config import settings
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import Notification
from app.schemas.types import SystemConfigKey, MessageChannel
from app.utils.string import StringUtils


class TorrentsChain(ChainBase):
    """
    种子刷新处理链
    """

    _cache_file = "__torrents_cache__"

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.siteshelper = SitesHelper()
        self.systemconfig = SystemConfigOper(self._db)

    def remote_refresh(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程刷新订阅，发送消息
        """
        self.post_message(Notification(channel=channel,
                                       title=f"开始刷新种子 ...", userid=userid))
        self.refresh()
        self.post_message(Notification(channel=channel,
                                       title=f"种子刷新完成！", userid=userid))

    def get_torrents(self) -> Dict[str, List[Context]]:
        """
        获取当前缓存的种子
        """
        # 读取缓存
        return self.load_cache(self._cache_file) or {}

    def refresh(self) -> Dict[str, List[Context]]:
        """
        刷新站点最新资源
        """
        # 读取缓存
        torrents_cache = self.get_torrents()

        # 所有站点索引
        indexers = self.siteshelper.get_indexers()
        # 配置的Rss站点
        config_indexers = [str(sid) for sid in self.systemconfig.get(SystemConfigKey.RssSites) or []]
        # 遍历站点缓存资源
        for indexer in indexers:
            # 未开启的站点不搜索
            if config_indexers and str(indexer.get("id")) not in config_indexers:
                continue
            logger.info(f'开始刷新 {indexer.get("name")} 最新种子 ...')
            domain = StringUtils.get_url_domain(indexer.get("domain"))
            torrents: List[TorrentInfo] = self.refresh_torrents(site=indexer)
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
                    # 识别媒体信息
                    mediainfo: MediaInfo = self.recognize_media(meta=meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
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
        self.save_cache(torrents_cache, self._cache_file)
        # 返回
        return torrents_cache
