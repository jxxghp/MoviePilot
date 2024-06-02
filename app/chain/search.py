import pickle
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict
from typing import List, Optional

from app.chain import ChainBase
from app.core.context import Context
from app.core.context import MediaInfo, TorrentInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.progress import ProgressHelper
from app.helper.sites import SitesHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType, ProgressKey, SystemConfigKey, EventType


class SearchChain(ChainBase):
    """
    站点资源搜索处理链
    """

    def __init__(self):
        super().__init__()
        self.siteshelper = SitesHelper()
        self.progress = ProgressHelper()
        self.systemconfig = SystemConfigOper()
        self.torrenthelper = TorrentHelper()

    def search_by_id(self, tmdbid: int = None, doubanid: str = None,
                     mtype: MediaType = None, area: str = "title", season: int = None) -> List[Context]:
        """
        根据TMDBID/豆瓣ID搜索资源，精确匹配，但不不过滤本地存在的资源
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        :param season: 季数
        """
        mediainfo = self.recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
        if not mediainfo:
            logger.error(f'{tmdbid} 媒体信息识别失败！')
            return []
        no_exists = None
        if season:
            no_exists = {
                tmdbid or doubanid: {
                    season: NotExistMediaInfo(episodes=[])
                }
            }
        results = self.process(mediainfo=mediainfo, area=area, no_exists=no_exists)
        # 保存结果
        bytes_results = pickle.dumps(results)
        self.systemconfig.set(SystemConfigKey.SearchResults, bytes_results)
        return results

    def search_by_title(self, title: str, page: int = 0, site: int = None) -> List[Context]:
        """
        根据标题搜索资源，不识别不过滤，直接返回站点内容
        :param title: 标题，为空时返回所有站点首页内容
        :param page: 页码
        :param site: 站点ID
        """
        if title:
            logger.info(f'开始搜索资源，关键词：{title} ...')
        else:
            logger.info(f'开始浏览资源，站点：{site} ...')
        # 搜索
        torrents = self.__search_all_sites(keywords=[title], sites=[site] if site else None, page=page) or []
        if not torrents:
            logger.warn(f'{title} 未搜索到资源')
            return []
        # 组装上下文
        contexts = [Context(meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                            torrent_info=torrent) for torrent in torrents]
        # 保存结果
        bytes_results = pickle.dumps(contexts)
        self.systemconfig.set(SystemConfigKey.SearchResults, bytes_results)
        return contexts

    def last_search_results(self) -> List[Context]:
        """
        获取上次搜索结果
        """
        results = self.systemconfig.get(SystemConfigKey.SearchResults)
        if not results:
            return []
        try:
            return pickle.loads(results)
        except Exception as e:
            logger.error(f'加载搜索结果失败：{str(e)} - {traceback.format_exc()}')
            return []

    def process(self, mediainfo: MediaInfo,
                keyword: str = None,
                no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                sites: List[int] = None,
                priority_rule: str = None,
                filter_rule: Dict[str, str] = None,
                area: str = "title") -> List[Context]:
        """
        根据媒体信息搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param priority_rule: 优先级规则，为空时使用搜索优先级规则
        :param filter_rule: 过滤规则，为空是使用默认过滤规则
        :param area: 搜索范围，title or imdbid
        """

        def __do_filter(torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
            """
            执行优先级过滤
            """
            return self.filter_torrents(rule_string=priority_rule,
                                        torrent_list=torrent_list,
                                        season_episodes=season_episodes,
                                        mediainfo=mediainfo) or []

        # 豆瓣标题处理
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = meta.begin_season
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')

        # 补充媒体信息
        if not mediainfo.names:
            mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                        tmdbid=mediainfo.tmdb_id,
                                                        doubanid=mediainfo.douban_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                return []

        # 缺失的季集
        mediakey = mediainfo.tmdb_id or mediainfo.douban_id
        if no_exists and no_exists.get(mediakey):
            # 过滤剧集
            season_episodes = {sea: info.episodes
                               for sea, info in no_exists[mediakey].items()}
        elif mediainfo.season:
            # 豆瓣只搜索当前季
            season_episodes = {mediainfo.season: []}
        else:
            season_episodes = None

        # 搜索关键词
        if keyword:
            keywords = [keyword]
        else:
            # 去重去空，但要保持顺序
            keywords = list(dict.fromkeys([k for k in [mediainfo.title,
                                                       mediainfo.original_title,
                                                       mediainfo.en_title,
                                                       mediainfo.sg_title] if k]))

        # 执行搜索
        torrents: List[TorrentInfo] = self.__search_all_sites(
            mediainfo=mediainfo,
            keywords=keywords,
            sites=sites,
            area=area
        )
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 未搜索到资源')
            return []

        # 开始新进度
        self.progress.start(ProgressKey.Search)

        # 开始匹配
        _match_torrents = []
        # 总数
        _total = len(torrents)
        # 已处理数
        _count = 0
        if mediainfo:
            # 英文标题应该在别名/原标题中，不需要再匹配
            logger.info(f"开始匹配结果 标题：{mediainfo.title}，原标题：{mediainfo.original_title}，别名：{mediainfo.names}")
            self.progress.update(value=0, text=f'开始匹配，总 {_total} 个资源 ...', key=ProgressKey.Search)
            for torrent in torrents:
                _count += 1
                self.progress.update(value=(_count / _total) * 96,
                                     text=f'正在匹配 {torrent.site_name}，已完成 {_count} / {_total} ...',
                                     key=ProgressKey.Search)
                if not torrent.title:
                    continue
                # 比对IMDBID
                if torrent.imdbid \
                        and mediainfo.imdb_id \
                        and torrent.imdbid == mediainfo.imdb_id:
                    logger.info(f'{mediainfo.title} 通过IMDBID匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 识别
                torrent_meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
                if torrent.title != torrent_meta.org_string:
                    logger.info(f"种子名称应用识别词后发生改变：{torrent.title} => {torrent_meta.org_string}")
                # 比对种子
                if self.torrenthelper.match_torrent(mediainfo=mediainfo,
                                                    torrent_meta=torrent_meta,
                                                    torrent=torrent):
                    # 匹配成功
                    _match_torrents.append(torrent)
                    continue
            # 匹配完成
            logger.info(f"匹配完成，共匹配到 {len(_match_torrents)} 个资源")
            self.progress.update(value=97,
                                 text=f'匹配完成，共匹配到 {len(_match_torrents)} 个资源',
                                 key=ProgressKey.Search)
        else:
            _match_torrents = torrents

        # 开始过滤
        self.progress.update(value=98, text=f'开始过滤，总 {len(_match_torrents)} 个资源，请稍候...',
                             key=ProgressKey.Search)

        # 开始过滤规则过滤
        if _match_torrents:
            logger.info(f'开始过滤规则过滤，当前规则：{filter_rule} ...')
            _match_torrents = self.filter_torrents_by_rule(torrents=_match_torrents,
                                                           mediainfo=mediainfo,
                                                           filter_rule=filter_rule)
        if not _match_torrents:
            logger.warn(f'{keyword or mediainfo.title} 没有符合过滤规则的资源')
            return []
        logger.info(f"过滤规则过滤完成，剩余 {len(_match_torrents)} 个资源")

        # 开始优先级规则/剧集过滤
        if priority_rule is None:
            # 取搜索优先级规则
            priority_rule = self.systemconfig.get(SystemConfigKey.SearchFilterRules)
        if priority_rule:
            logger.info(f'开始优先级规则/剧集过滤，当前规则：{priority_rule} ...')
            _match_torrents = __do_filter(_match_torrents)
            if not _match_torrents:
                logger.warn(f'{keyword or mediainfo.title} 没有符合优先级规则的资源')
                return []
            logger.info(f"优先级规则/剧集过滤完成，剩余 {len(_match_torrents)} 个资源")

        # 去掉mediainfo中多余的数据
        mediainfo.clear()

        # 组装上下文
        contexts = [Context(meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                            media_info=mediainfo,
                            torrent_info=torrent) for torrent in _match_torrents]

        self.progress.update(value=99, text=f'过滤完成，剩余 {len(contexts)} 个资源', key=ProgressKey.Search)

        # 排序
        self.progress.update(value=99,
                             text=f'正在对 {len(contexts)} 个资源进行排序，请稍候...',
                             key=ProgressKey.Search)
        contexts = self.torrenthelper.sort_torrents(contexts)

        # 结束进度
        self.progress.update(value=100,
                             text=f'搜索完成，共 {len(contexts)} 个资源',
                             key=ProgressKey.Search)
        logger.info(f'搜索完成，共 {len(contexts)} 个资源')
        self.progress.end(ProgressKey.Search)

        # 返回
        return contexts

    def __search_all_sites(self, keywords: List[str],
                           mediainfo: Optional[MediaInfo] = None,
                           sites: List[int] = None,
                           page: int = 0,
                           area: str = "title") -> Optional[List[TorrentInfo]]:
        """
        多线程搜索多个站点
        :param mediainfo:  识别的媒体信息
        :param keywords:  搜索关键词列表
        :param sites:  指定站点ID列表，如有则只搜索指定站点，否则搜索所有站点
        :param page:  搜索页码
        :param area:  搜索区域 title or imdbid
        :reutrn: 资源列表
        """
        # 未开启的站点不搜索
        indexer_sites = []

        # 配置的索引站点
        if not sites:
            sites = self.systemconfig.get(SystemConfigKey.IndexerSites) or []

        for indexer in self.siteshelper.get_indexers():
            # 检查站点索引开关
            if not sites or indexer.get("id") in sites:
                # 站点流控
                state, msg = self.siteshelper.check(indexer.get("domain"))
                if state:
                    logger.warn(msg)
                    continue
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            return []

        # 开始进度
        self.progress.start(ProgressKey.Search)
        # 开始计时
        start_time = datetime.now()
        # 总数
        total_num = len(indexer_sites)
        # 完成数
        finish_count = 0
        # 更新进度
        self.progress.update(value=0,
                             text=f"开始搜索，共 {total_num} 个站点 ...",
                             key=ProgressKey.Search)
        # 多线程
        executor = ThreadPoolExecutor(max_workers=len(indexer_sites))
        all_task = []
        for site in indexer_sites:
            if area == "imdbid":
                # 搜索IMDBID
                task = executor.submit(self.search_torrents, site=site,
                                       keywords=[mediainfo.imdb_id] if mediainfo else None,
                                       mtype=mediainfo.type if mediainfo else None,
                                       page=page)
            else:
                # 搜索标题
                task = executor.submit(self.search_torrents, site=site,
                                       keywords=keywords,
                                       mtype=mediainfo.type if mediainfo else None,
                                       page=page)
            all_task.append(task)
        # 结果集
        results = []
        for future in as_completed(all_task):
            finish_count += 1
            result = future.result()
            if result:
                results.extend(result)
            logger.info(f"站点搜索进度：{finish_count} / {total_num}")
            self.progress.update(value=finish_count / total_num * 100,
                                 text=f"正在搜索{keywords or ''}，已完成 {finish_count} / {total_num} 个站点 ...",
                                 key=ProgressKey.Search)
        # 计算耗时
        end_time = datetime.now()
        # 更新进度
        self.progress.update(value=100,
                             text=f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒",
                             key=ProgressKey.Search)
        logger.info(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        # 结束进度
        self.progress.end(ProgressKey.Search)
        # 返回
        return results

    def filter_torrents_by_rule(self,
                                torrents: List[TorrentInfo],
                                mediainfo: MediaInfo,
                                filter_rule: Dict[str, str] = None,
                                ) -> List[TorrentInfo]:
        """
        使用过滤规则过滤种子
        :param torrents: 种子列表
        :param filter_rule: 过滤规则
        :param mediainfo: 媒体信息
        """

        if not filter_rule:
            # 没有则取搜索默认过滤规则
            filter_rule = self.systemconfig.get(SystemConfigKey.DefaultSearchFilterRules)
        if not filter_rule:
            return torrents

        # 使用默认过滤规则再次过滤
        return list(filter(
            lambda t: self.torrenthelper.filter_torrent(
                torrent_info=t,
                filter_rule=filter_rule,
                mediainfo=mediainfo
            ),
            torrents
        ))

    @eventmanager.register(EventType.SiteDeleted)
    def remove_site(self, event: Event):
        """
        从搜索站点中移除与已删除站点相关的设置
        """
        if not event:
            return
        event_data = event.event_data or {}
        site_id = event_data.get("site_id")
        if not site_id:
            return
        if site_id == "*":
            # 清空搜索站点
            SystemConfigOper().set(SystemConfigKey.IndexerSites, [])
            return
        # 从选中的rss站点中移除
        selected_sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
        if site_id in selected_sites:
            selected_sites.remove(site_id)
            SystemConfigOper().set(SystemConfigKey.IndexerSites, selected_sites)
