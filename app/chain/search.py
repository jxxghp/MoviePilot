from typing import Optional, List

from app.chain import ChainBase
from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.core.meta_info import MetaInfo
from app.helper.sites import SitesHelper
from app.log import logger


class SearchChain(ChainBase):
    """
    站点资源搜索处理链
    """

    def __init__(self):
        super().__init__()
        self.siteshelper = SitesHelper()

    def process(self, meta: MetaBase, mediainfo: MediaInfo,
                keyword: str = None) -> Optional[List[Context]]:
        """
        根据媒体信息，执行搜索
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        """
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')
        # 未开启的站点不搜索
        indexer_sites = []
        for indexer in self.siteshelper.get_indexers():
            if not settings.INDEXER_SITES \
                    or any([s in indexer.get("domain") for s in settings.INDEXER_SITES.split(',')]):
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            return []
        # 执行搜索
        torrents: List[TorrentInfo] = self.search_torrents(
            mediainfo=mediainfo,
            keyword=keyword,
            sites=indexer_sites
        )
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 未搜索到资源')
            return []
        # 过滤种子
        result: List[TorrentInfo] = self.filter_torrents(torrent_list=torrents)
        if result is not None:
            torrents = result
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 没有符合过滤条件的资源')
            return []
        # 过滤不匹配的资源
        _match_torrents = []
        if mediainfo:
            for torrent in torrents:
                # 比对IMDBID
                if torrent.imdbid \
                        and mediainfo.imdb_id \
                        and torrent.imdbid == mediainfo.imdb_id:
                    _match_torrents.append(torrent)
                    continue
                # 识别
                torrent_meta = MetaInfo(torrent.title, torrent.description)
                # 识别媒体信息
                torrent_mediainfo: MediaInfo = self.recognize_media(meta=torrent_meta)
                if not torrent_mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
                    continue
                # 过滤
                if torrent_mediainfo.tmdb_id == mediainfo.tmdb_id \
                        and torrent_mediainfo.type == mediainfo.type:
                    logger.info(f'{mediainfo.title} 匹配到资源：{torrent.title}')
                    _match_torrents.append(torrent)
        else:
            _match_torrents = torrents
        # 组装上下文返回
        return [Context(meta=MetaInfo(torrent.title),
                        mediainfo=mediainfo,
                        torrentinfo=torrent) for torrent in _match_torrents]
