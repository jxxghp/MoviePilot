from typing import Dict, List, Optional

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.core.metainfo import MetaInfo
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.config import settings
from app.db.subscribes import Subscribes
from app.helper.sites import SitesHelper
from app.log import logger
from app.utils.string import StringUtils
from app.utils.types import MediaType


class SubscribeChain(ChainBase):
    """
    订阅处理链
    """

    # 站点最新种子缓存 {站点域名: 种子上下文}
    _torrents_cache: Dict[str, List[Context]] = {}

    def __init__(self):
        super().__init__()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribes = Subscribes()
        self.siteshelper = SitesHelper()

    def process(self, title: str,
                mtype: MediaType = None,
                tmdbid: int = None,
                season: int = None,
                userid: str = None,
                username: str = None,
                **kwargs) -> bool:
        """
        识别媒体信息并添加订阅
        """
        logger.info(f'开始添加订阅，标题：{title} ...')
        # 识别前预处理
        result: Optional[tuple] = self.prepare_recognize(title=title)
        if result:
            title, _ = result
        # 识别元数据
        metainfo = MetaInfo(title)
        if mtype:
            metainfo.type = mtype
        if season:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo, mtype=mtype, tmdbid=tmdbid)
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}')
            return False
        # 更新媒体图片
        self.obtain_image(mediainfo=mediainfo)
        # 总集数
        if mediainfo.type == MediaType.TV:
            if not kwargs.get('total_episode'):
                if not mediainfo.seasons:
                    # 补充媒体信息
                    mediainfo: MediaInfo = self.recognize_media(meta=MetaInfo(title=mediainfo.get_title_string()),
                                                                mtype=mediainfo.type,
                                                                tmdbid=mediainfo.tmdb_id)
                    if not mediainfo:
                        logger.error(f"媒体信息识别失败！")
                        return False
                    if not mediainfo.seasons:
                        logger.error(f"媒体信息中没有季集信息，标题：{title}，tmdbid：{tmdbid}")
                        return False
                total_episode = len(mediainfo.seasons.get(kwargs.get('season') or 1) or [])
                if not total_episode:
                    logger.error(f'未获取到总集数，标题：{title}，tmdbid：{tmdbid}')
                    return False
                kwargs.update({
                    'total_episode': total_episode
                })
        # 添加订阅
        state, err_msg = self.subscribes.add(mediainfo, season=season, **kwargs)
        if not state:
            logger.info(f'{mediainfo.get_title_string()} {err_msg}')
            # 发回原用户
            self.post_message(title=f"{mediainfo.get_title_string()}{metainfo.get_season_string()} "
                                    f"添加订阅失败！",
                              text=f"{err_msg}",
                              image=mediainfo.get_message_image(),
                              userid=userid)
        else:
            logger.error(f'{mediainfo.get_title_string()} 添加订阅成功')
            # 广而告之
            self.post_message(title=f"{mediainfo.get_title_string()}{metainfo.get_season_string()} 已添加订阅",
                              text=f"来自用户：{username or userid}",
                              image=mediainfo.get_message_image())
        # 返回结果
        return state

    def search(self, sid: int = None, state: str = 'N'):
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:未搜索 R:已搜索
        :return: 更新订阅状态为R或删除订阅
        """
        if sid:
            subscribes = [self.subscribes.get(sid)]
        else:
            subscribes = self.subscribes.list(state)
        # 遍历订阅
        for subscribe in subscribes:
            logger.info(f'开始搜索订阅，标题：{subscribe.name} ...')
            # 如果状态为N则更新为R
            if subscribe.state == 'N':
                self.subscribes.update(subscribe.id, {'state': 'R'})
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season
            meta.type = MediaType.MOVIE if subscribe.type == MediaType.MOVIE.value else MediaType.TV
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue
            # 查询缺失的媒体信息
            exist_flag, no_exists = self.downloadchain.get_no_exists_info(mediainfo=mediainfo)
            if exist_flag:
                logger.info(f'{mediainfo.get_title_string()} 媒体库中已存在，完成订阅')
                self.subscribes.delete(subscribe.id)
                # 发送通知
                self.post_message(title=f'{mediainfo.get_title_string()}{meta.get_season_string()} 已完成订阅',
                                  image=mediainfo.get_message_image())
                continue
            # 搜索
            contexts = self.searchchain.process(meta=meta,
                                                mediainfo=mediainfo,
                                                keyword=subscribe.keyword,
                                                no_exists=no_exists)
            if not contexts:
                logger.warn(f'{subscribe.keyword or subscribe.name} 未搜索到资源')
                continue
            # 自动下载
            downloads, lefts = self.downloadchain.batch_download(contexts=contexts, need_tvs=no_exists)
            if downloads and not lefts:
                # 全部下载完成
                logger.info(f'{mediainfo.get_title_string()} 下载完成，完成订阅')
                self.subscribes.delete(subscribe.id)
                # 发送通知
                self.post_message(title=f'{mediainfo.get_title_string()}{meta.get_season_string()} 已完成订阅',
                                  image=mediainfo.get_message_image())
            else:
                # 未完成下载
                logger.info(f'{mediainfo.get_title_string()} 未下载未完整，继续订阅 ...')

    def refresh(self):
        """
        刷新站点最新资源
        """
        # 所有站点索引
        indexers = self.siteshelper.get_indexers()
        # 遍历站点缓存资源
        for indexer in indexers:
            # 未开启的站点不搜索
            if settings.INDEXER_SITES \
                    and not any([s in indexer.get("domain") for s in settings.INDEXER_SITES.split(',')]):
                continue
            logger.info(f'开始刷新站点资源，站点：{indexer.get("name")} ...')
            domain = StringUtils.get_url_domain(indexer.get("domain"))
            torrents: List[TorrentInfo] = self.refresh_torrents(sites=[indexer])
            if torrents:
                self._torrents_cache[domain] = []
                # 过滤种子
                result: List[TorrentInfo] = self.filter_torrents(torrent_list=torrents)
                if result is not None:
                    torrents = result
                if not torrents:
                    logger.warn(f'{indexer.get("name")} 没有符合过滤条件的资源')
                    continue
                for torrent in torrents:
                    # 识别
                    meta = MetaInfo(torrent.title, torrent.description)
                    # 识别媒体信息
                    mediainfo: MediaInfo = self.recognize_media(meta=meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
                        continue
                    # 上下文
                    context = Context(meta=meta, mediainfo=mediainfo, torrentinfo=torrent)
                    self._torrents_cache[domain].append(context)
        # 从缓存中匹配订阅
        self.match()

    def match(self):
        """
        从缓存中匹配订阅，并自动下载
        """
        # 所有订阅
        subscribes = self.subscribes.list('R')
        # 遍历订阅
        for subscribe in subscribes:
            logger.info(f'开始匹配订阅，标题：{subscribe.name} ...')
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season
            meta.type = MediaType.MOVIE if subscribe.type == MediaType.MOVIE.value else MediaType.TV
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue
            # 查询缺失的媒体信息
            exist_flag, no_exists = self.downloadchain.get_no_exists_info(mediainfo=mediainfo)
            if exist_flag:
                logger.info(f'{mediainfo.get_title_string()} 媒体库中已存在，完成订阅')
                self.subscribes.delete(subscribe.id)
                # 发送通知
                self.post_message(title=f'{mediainfo.get_title_string()}{meta.get_season_string()} 已完成订阅',
                                  image=mediainfo.get_message_image())
                continue
            # 遍历缓存种子
            _match_context = []
            for domain, contexts in self._torrents_cache.items():
                for context in contexts:
                    # 检查是否匹配
                    torrent_meta = context.meta_info
                    torrent_mediainfo = context.media_info
                    torrent_info = context.torrent_info
                    if torrent_mediainfo.tmdb_id == mediainfo.tmdb_id \
                            and torrent_mediainfo.type == mediainfo.type:
                        if meta.begin_season and meta.begin_season != torrent_meta.begin_season:
                            continue
                        # 匹配成功
                        logger.info(f'{mediainfo.get_title_string()} 匹配成功：{torrent_info.title}')
                        _match_context.append(context)
            logger.info(f'{mediainfo.get_title_string()} 匹配完成，共匹配到{len(_match_context)}个资源')
            if _match_context:
                # 批量择优下载
                downloads, lefts = self.downloadchain.batch_download(contexts=_match_context, need_tvs=no_exists)
                if downloads and not lefts:
                    # 全部下载完成
                    logger.info(f'{mediainfo.get_title_string()} 下载完成，完成订阅')
                    self.subscribes.delete(subscribe.id)
                    # 发送通知
                    self.post_message(title=f'{mediainfo.get_title_string()}{meta.get_season_string()} 已完成订阅',
                                      image=mediainfo.get_message_image())
                else:
                    # 未完成下载，计算剩余集数
                    left_episodes = lefts.get(mediainfo.tmdb_id, {}).get("episodes", [])
                    logger.info(f'{mediainfo.get_title_string()} 未下载未完整，更新缺失集数为{len(left_episodes)} ...')
                    self.subscribes.update(subscribe.id, {
                        "lack_episode": len(left_episodes)
                    })
