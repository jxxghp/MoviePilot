import pickle
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict
from typing import List, Optional

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

    def __init__(self):
        super().__init__()
        self.siteshelper = SitesHelper()
        self.progress = ProgressHelper()
        self.systemconfig = SystemConfigOper()
        self.torrenthelper = TorrentHelper()

    def search_by_id(self, tmdbid: int = None, doubanid: str = None,
                     mtype: MediaType = None, area: str = "title") -> List[Context]:
        """
        根据TMDBID/豆瓣ID搜索资源，精确匹配，但不不过滤本地存在的资源
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        """
        mediainfo = self.recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
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
        return self.__search_all_sites(keywords=[title], sites=[site] if site else None, page=page) or []

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
                               for sea, info in no_exists[mediainfo.tmdb_id].items()}
        elif mediainfo.season:
            # 豆瓣只搜索当前季
            season_episodes = {mediainfo.season: []}
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
        torrents: List[TorrentInfo] = self.__search_all_sites(
            mediainfo=mediainfo,
            keywords=keywords,
            sites=sites,
            area=area
        )
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 未搜索到资源')
            return []
        # 过滤种子
        if priority_rule is None:
            # 取搜索优先级规则
            priority_rule = self.systemconfig.get(SystemConfigKey.SearchFilterRules)
        if priority_rule:
            logger.info(f'开始过滤资源，当前规则：{priority_rule} ...')
            result: List[TorrentInfo] = self.filter_torrents(rule_string=priority_rule,
                                                             torrent_list=torrents,
                                                             season_episodes=season_episodes,
                                                             mediainfo=mediainfo)
            if result is not None:
                torrents = result
            if not torrents:
                logger.warn(f'{keyword or mediainfo.title} 没有符合优先级规则的资源')
                return []
        # 使用过滤规则再次过滤
        torrents = self.filter_torrents_by_rule(torrents=torrents,
                                                filter_rule=filter_rule)
        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 没有符合过滤规则的资源')
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
            logger.info(f"标题：{mediainfo.title}，原标题：{mediainfo.original_title}，别名：{mediainfo.names}")
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
                # 比对标题和原语种标题
                meta_name = StringUtils.clear_upper(torrent_meta.name)
                if meta_name in [
                    StringUtils.clear_upper(mediainfo.title),
                    StringUtils.clear_upper(mediainfo.original_title)
                ]:
                    logger.info(f'{mediainfo.title} 通过标题匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append(torrent)
                    continue
                # 在副标题中判断是否存在标题与原语种标题
                if torrent.description:
                    subtitle = re.split(r'[\s/|]+', torrent.description)
                    if (StringUtils.is_chinese(mediainfo.title)
                        and str(mediainfo.title) in subtitle) \
                            or (StringUtils.is_chinese(mediainfo.original_title)
                                and str(mediainfo.original_title) in subtitle):
                        logger.info(f'{mediainfo.title} 通过副标题匹配到资源：{torrent.site_name} - {torrent.title}，'
                                    f'副标题：{torrent.description}')
                        _match_torrents.append(torrent)
                        continue
                # 比对别名和译名
                for name in mediainfo.names:
                    if StringUtils.clear_upper(name) == meta_name:
                        logger.info(f'{mediainfo.title} 通过别名或译名匹配到资源：{torrent.site_name} - {torrent.title}')
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
                                filter_rule: Dict[str, str] = None
                                ) -> List[TorrentInfo]:
        """
        使用过滤规则过滤种子
        :param torrents: 种子列表
        :param filter_rule: 过滤规则
        """

        if not filter_rule:
            # 没有则取搜索默认过滤规则
            filter_rule = self.systemconfig.get(SystemConfigKey.DefaultSearchFilterRules)
        if not filter_rule:
            return torrents
        # 包含
        include = filter_rule.get("include")
        # 排除
        exclude = filter_rule.get("exclude")
        # 质量
        quality = filter_rule.get("quality")
        # 分辨率
        resolution = filter_rule.get("resolution")
        # 特效
        effect = filter_rule.get("effect")

        def __filter_torrent(t: TorrentInfo) -> bool:
            """
            过滤种子
            """
            # 包含
            if include:
                if not re.search(r"%s" % include,
                                 f"{t.title} {t.description}", re.I):
                    logger.info(f"{t.title} 不匹配包含规则 {include}")
                    return False
            # 排除
            if exclude:
                if re.search(r"%s" % exclude,
                             f"{t.title} {t.description}", re.I):
                    logger.info(f"{t.title} 匹配排除规则 {exclude}")
                    return False
            # 质量
            if quality:
                if not re.search(r"%s" % quality, t.title, re.I):
                    logger.info(f"{t.title} 不匹配质量规则 {quality}")
                    return False

            # 分辨率
            if resolution:
                if not re.search(r"%s" % resolution, t.title, re.I):
                    logger.info(f"{t.title} 不匹配分辨率规则 {resolution}")
                    return False

            # 特效
            if effect:
                if not re.search(r"%s" % effect, t.title, re.I):
                    logger.info(f"{t.title} 不匹配特效规则 {effect}")
                    return False

            return True

        # 使用默认过滤规则再次过滤
        return list(filter(lambda t: __filter_torrent(t), torrents))
