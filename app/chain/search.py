from typing import Optional, List, Dict

from app.chain import ChainBase
from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas.context import NotExistMediaInfo
from app.utils.string import StringUtils
from app.utils.types import MediaType


class SearchChain(ChainBase):
    """
    站点资源搜索处理链
    """

    def __init__(self):
        super().__init__()
        self.siteshelper = SitesHelper()

    def process(self, meta: MetaBase, mediainfo: MediaInfo,
                keyword: str = None,
                no_exists: Dict[int, List[NotExistMediaInfo]] = None) -> Optional[List[Context]]:
        """
        根据媒体信息，执行搜索
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        """
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')
        # 未开启的站点不搜索
        indexer_sites = []
        for indexer in self.siteshelper.get_indexers():
            if not settings.INDEXER_SITES \
                    or any([s in indexer.get("domain") for s in settings.INDEXER_SITES.split(',')]):
                # 站点流控
                state, msg = self.siteshelper.check(indexer.get("domain"))
                if not state:
                    logger.warn(msg)
                    continue
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            return []
        # 补充媒体信息
        if not mediainfo.names:
            mediainfo: MediaInfo = self.recognize_media(meta=meta,
                                                        mtype=mediainfo.type,
                                                        tmdbid=mediainfo.tmdb_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                return []
        # 缺失的媒体信息
        if no_exists:
            # 过滤剧集
            season_episodes = {info.get('season'): info.get('episodes')
                               for info in no_exists.get(mediainfo.tmdb_id)}
        else:
            season_episodes = None
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
        logger.info(f'开始过滤资源，当前规则：{settings.FILTER_RULE} ...')
        result: List[TorrentInfo] = self.filter_torrents(torrent_list=torrents,
                                                         season_episodes=season_episodes)
        if result is not None:
            torrents = result
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 没有符合过滤条件的资源')
            return []
        # 过滤不匹配的资源
        logger.info(f'开始匹配，总 {len(torrents)} 个资源 ...')
        _match_torrents = []
        if mediainfo:
            for torrent in torrents:
                # 比对IMDBID
                if torrent.imdbid \
                        and mediainfo.imdb_id \
                        and torrent.imdbid == mediainfo.imdb_id:
                    logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 识别前预处理
                result: Optional[tuple] = self.prepare_recognize(title=torrent.title, subtitle=torrent.description)
                if result:
                    title, subtitle = result
                else:
                    title, subtitle = torrent.title, torrent.description
                # 识别
                torrent_meta = MetaInfo(title=title, subtitle=subtitle)
                # 比对年份
                if torrent_meta.year and mediainfo.year:
                    if mediainfo.type == MediaType.TV:
                        # 剧集
                        if torrent_meta.year not in [year for year in mediainfo.season_years.values()]:
                            continue
                    else:
                        # 没有季的剧集或者电影
                        if torrent_meta.year != mediainfo.year:
                            continue
                # 比对标题
                if torrent_meta.name in [mediainfo.title, mediainfo.original_title]:
                    logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 比对别名和译名
                for name in mediainfo.names:
                    if StringUtils.clear(name).strip().upper() == \
                            StringUtils.clear(torrent_meta.name).strip().upper():
                        logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                        _match_torrents.append(torrent)
                        break
        else:
            _match_torrents = torrents
        logger.info(f"匹配完成，共匹配到 {len(_match_torrents)} 个资源")
        # 组装上下文返回
        return [Context(meta=MetaInfo(torrent.title),
                        mediainfo=mediainfo,
                        torrentinfo=torrent) for torrent in _match_torrents]
