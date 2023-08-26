import pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict
from typing import List, Optional

from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.core.context import Context
from app.core.context import MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.progress import ProgressHelper
from app.helper.sites import SitesHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType, ProgressKey, SystemConfigKey
from app.utils.string import StringUtils


class SearchChain(ChainBase):
    """
    站点资源搜索处理链
    """

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.siteshelper = SitesHelper()
        self.progress = ProgressHelper()
        self.systemconfig = SystemConfigOper(self._db)
        self.torrenthelper = TorrentHelper()

    def search_by_tmdbid(self, tmdbid: int, mtype: MediaType = None, area: str = "title") -> List[Context]:
        """
        根据TMDB ID搜索资源，精确匹配，但不不过滤本地存在的资源
        :param tmdbid: TMDB ID
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        """
        mediainfo = self.recognize_media(tmdbid=tmdbid, mtype=mtype)
        if not mediainfo:
            logger.error(f'{tmdbid} 媒体信息识别失败！')
            return []
        results = self.process(mediainfo=mediainfo, area=area)
        # 保存眲结果
        bytes_results = pickle.dumps(results)
        self.systemconfig.set(SystemConfigKey.SearchResults, bytes_results)
        return results

    def search_by_title(self, title: str, page: int = 0, site: int = None) -> List[TorrentInfo]:
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
        return self.__search_all_sites(keyword=title, sites=[site] if site else None, page=page) or []

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
            print(str(e))
            return []

    def browse(self, domain: str, keyword: str = None) -> List[TorrentInfo]:
        """
        浏览站点首页内容
        :param domain: 站点域名
        :param keyword: 关键词，有值时为搜索
        """
        if not keyword:
            logger.info(f'开始浏览站点首页内容，站点：{domain} ...')
        else:
            logger.info(f'开始搜索资源，关键词：{keyword}，站点：{domain} ...')
        site = self.siteshelper.get_indexer(domain)
        if not site:
            logger.error(f'站点 {domain} 不存在！')
            return []
        return self.search_torrents(site=site, keyword=keyword)

    def process(self, mediainfo: MediaInfo,
                keyword: str = None,
                no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                sites: List[int] = None,
                filter_rule: str = None,
                area: str = "title") -> List[Context]:
        """
        根据媒体信息搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param filter_rule: 过滤规则，为空是使用默认过滤规则
        :param area: 搜索范围，title or imdbid
        """
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')
        # 补充媒体信息
        if not mediainfo.names:
            mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                        tmdbid=mediainfo.tmdb_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                return []
        # 缺失的季集
        if no_exists and no_exists.get(mediainfo.tmdb_id):
            # 过滤剧集
            season_episodes = {sea: info.episodes
                               for sea, info in no_exists[mediainfo.tmdb_id].items()}
        else:
            season_episodes = None
        # 搜索关键词
        if keyword:
            keywords = [keyword]
        elif mediainfo.original_title and mediainfo.title != mediainfo.original_title:
            keywords = [mediainfo.title, mediainfo.original_title]
        else:
            keywords = [mediainfo.title]
        # 执行搜索
        torrents: List[TorrentInfo] = []
        for keyword in keywords:
            torrents = self.__search_all_sites(
                mediainfo=mediainfo,
                keyword=keyword,
                sites=sites,
                area=area
            )
            if torrents:
                break
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 未搜索到资源')
            return []
        # 过滤种子
        if filter_rule is None:
            # 取默认过滤规则
            filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules)
        if filter_rule:
            logger.info(f'开始过滤资源，当前规则：{filter_rule} ...')
            result: List[TorrentInfo] = self.filter_torrents(rule_string=filter_rule,
                                                             torrent_list=torrents,
                                                             season_episodes=season_episodes)
            if result is not None:
                torrents = result
            if not torrents:
                logger.warn(f'{keyword or mediainfo.title} 没有符合过滤条件的资源')
                return []
        # 匹配的资源
        _match_torrents = []
        # 总数
        _total = len(torrents)
        # 已处理数
        _count = 0
        if mediainfo:
            self.progress.start(ProgressKey.Search)
            logger.info(f'开始匹配，总 {_total} 个资源 ...')
            self.progress.update(value=0, text=f'开始匹配，总 {_total} 个资源 ...', key=ProgressKey.Search)
            for torrent in torrents:
                _count += 1
                self.progress.update(value=(_count / _total) * 100,
                                     text=f'正在匹配 {torrent.site_name}，已完成 {_count} / {_total} ...',
                                     key=ProgressKey.Search)
                # 比对IMDBID
                if torrent.imdbid \
                        and mediainfo.imdb_id \
                        and torrent.imdbid == mediainfo.imdb_id:
                    logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 识别
                torrent_meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
                # 比对类型
                if (torrent_meta.type == MediaType.TV and mediainfo.type != MediaType.TV) \
                        or (torrent_meta.type != MediaType.TV and mediainfo.type == MediaType.TV):
                    logger.warn(f'{torrent.site_name} - {torrent.title} 类型不匹配')
                    continue
                # 比对年份
                if mediainfo.year:
                    if mediainfo.type == MediaType.TV:
                        # 剧集年份，每季的年份可能不同
                        if torrent_meta.year and torrent_meta.year not in [year for year in
                                                                           mediainfo.season_years.values()]:
                            logger.warn(f'{torrent.site_name} - {torrent.title} 年份不匹配')
                            continue
                    else:
                        # 电影年份，上下浮动1年
                        if torrent_meta.year not in [str(int(mediainfo.year) - 1),
                                                     mediainfo.year,
                                                     str(int(mediainfo.year) + 1)]:
                            logger.warn(f'{torrent.site_name} - {torrent.title} 年份不匹配')
                            continue
                # 比对标题
                meta_name = StringUtils.clear_upper(torrent_meta.name)
                if meta_name in [
                    StringUtils.clear_upper(mediainfo.title),
                    StringUtils.clear_upper(mediainfo.original_title)
                ]:
                    logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 比对别名和译名
                for name in mediainfo.names:
                    if StringUtils.clear_upper(name) == meta_name:
                        logger.info(f'{mediainfo.title} 匹配到资源：{torrent.site_name} - {torrent.title}')
                        _match_torrents.append(torrent)
                        break
                else:
                    logger.warn(f'{torrent.site_name} - {torrent.title} 标题不匹配')
            self.progress.update(value=100,
                                 text=f'匹配完成，共匹配到 {len(_match_torrents)} 个资源',
                                 key=ProgressKey.Search)
            self.progress.end(ProgressKey.Search)
        else:
            _match_torrents = torrents
        logger.info(f"匹配完成，共匹配到 {len(_match_torrents)} 个资源")
        # 去掉mediainfo中多余的数据
        mediainfo.clear()
        # 组装上下文
        contexts = [Context(meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                            media_info=mediainfo,
                            torrent_info=torrent) for torrent in _match_torrents]
        # 排序
        contexts = self.torrenthelper.sort_torrents(contexts)
        # 返回
        return contexts

    def __search_all_sites(self, mediainfo: Optional[MediaInfo] = None,
                           keyword: str = None,
                           sites: List[int] = None,
                           page: int = 0,
                           area: str = "title") -> Optional[List[TorrentInfo]]:
        """
        多线程搜索多个站点
        :param mediainfo:  识别的媒体信息
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :param sites:  指定站点ID列表，如有则只搜索指定站点，否则搜索所有站点
        :param page:  搜索页码
        :param area:  搜索区域 title or imdbid
        :reutrn: 资源列表
        """
        # 未开启的站点不搜索
        indexer_sites = []
        # 配置的索引站点
        if sites:
            config_indexers = [str(sid) for sid in sites]
        else:
            config_indexers = [str(sid) for sid in self.systemconfig.get(SystemConfigKey.IndexerSites) or []]
        for indexer in self.siteshelper.get_indexers():
            # 检查站点索引开关
            if not config_indexers or str(indexer.get("id")) in config_indexers:
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
            task = executor.submit(self.search_torrents, mediainfo=mediainfo,
                                   site=site, keyword=keyword, page=page, area=area)
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
                                 text=f"正在搜索{keyword or ''}，已完成 {finish_count} / {total_num} 个站点 ...",
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
