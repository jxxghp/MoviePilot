import json
import re
import time
from datetime import datetime
from typing import Tuple, Optional

from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.context import Context, TorrentInfo, MediaInfo
from app.core.metainfo import MetaInfo
from app.db.rss_oper import RssOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.helper.rss import RssHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import Notification, NotExistMediaInfo
from app.schemas.types import SystemConfigKey, MediaType, NotificationType
from app.utils.string import StringUtils


class RssChain(ChainBase):
    """
    RSS处理链
    """

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.rssoper = RssOper(self._db)
        self.sites = SitesHelper()
        self.systemconfig = SystemConfigOper(self._db)
        self.downloadchain = DownloadChain(self._db)
        self.message = MessageHelper()

    def add(self, title: str, year: str,
            mtype: MediaType = None,
            season: int = None,
            **kwargs) -> Tuple[Optional[int], str]:
        """
        识别媒体信息并添加订阅
        """
        logger.info(f'开始添加自定义订阅，标题：{title} ...')
        # 识别元数据
        metainfo = MetaInfo(title)
        if year:
            metainfo.year = year
        if mtype:
            metainfo.type = mtype
        if season:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo)
        if not mediainfo:
            logger.warn(f'{title} 未识别到媒体信息')
            return None, "未识别到媒体信息"
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 总集数
        if mediainfo.type == MediaType.TV:
            if not season:
                season = 1
            # 总集数
            if not kwargs.get('total_episode'):
                if not mediainfo.seasons:
                    # 补充媒体信息
                    mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                                tmdbid=mediainfo.tmdb_id)
                    if not mediainfo:
                        logger.error(f"媒体信息识别失败！")
                        return None, "媒体信息识别失败"
                    if not mediainfo.seasons:
                        logger.error(f"{title} 媒体信息中没有季集信息")
                        return None, "媒体信息中没有季集信息"
                total_episode = len(mediainfo.seasons.get(season) or [])
                if not total_episode:
                    logger.error(f'{title} 未获取到总集数')
                    return None, "未获取到总集数"
                kwargs.update({
                    'total_episode': total_episode
                })
        # 检查是否存在
        if self.rssoper.exists(tmdbid=mediainfo.tmdb_id, season=season):
            logger.warn(f'{mediainfo.title} 已存在')
            return None, f'{mediainfo.title} 自定义订阅已存在'
        if not kwargs.get("name"):
            kwargs.update({
                "name": mediainfo.title
            })
        kwargs.update({
            "tmdbid": mediainfo.tmdb_id,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview,
        })
        # 添加订阅
        sid = self.rssoper.add(title=title, year=year, season=season, **kwargs)
        if not sid:
            logger.error(f'{mediainfo.title_year} 添加自定义订阅失败')
            return None, "添加自定义订阅失败"
        else:
            logger.info(f'{mediainfo.title_year} {metainfo.season} 添加订阅成功')

        # 返回结果
        return sid, ""

    def refresh(self, rssid: int = None, manual: bool = False):
        """
        刷新RSS订阅数据
        """
        # 所有RSS订阅
        logger.info("开始刷新RSS订阅数据 ...")
        rss_tasks = self.rssoper.list(rssid) or []
        for rss_task in rss_tasks:
            if not rss_task:
                continue
            if not rss_task.url:
                continue
            # 下载Rss报文
            items = RssHelper.parse(rss_task.url, True if rss_task.proxy else False)
            if not items:
                logger.error(f"RSS未下载到数据：{rss_task.url}")
            logger.info(f"{rss_task.name} RSS下载到数据：{len(items)}")
            # 检查站点
            domain = StringUtils.get_url_domain(rss_task.url)
            site_info = self.sites.get_indexer(domain) or {}
            # 过滤规则
            if rss_task.best_version:
                filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules2)
            else:
                filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules)
            # 处理RSS条目
            matched_contexts = []
            # 处理过的title
            processed_data = json.loads(rss_task.note) if rss_task.note else {
                "titles": [],
                "season_episodes": []
            }
            for item in items:
                if not item.get("title"):
                    continue
                # 标题是否已处理过
                if item.get("title") in processed_data.get('titles'):
                    logger.info(f"{item.get('title')} 已处理过")
                    continue
                # 基本要素匹配
                if rss_task.include \
                        and not re.search(r"%s" % rss_task.include, item.get("title")):
                    logger.info(f"{item.get('title')} 未包含 {rss_task.include}")
                    continue
                if rss_task.exclude \
                        and re.search(r"%s" % rss_task.exclude, item.get("title")):
                    logger.info(f"{item.get('title')} 包含 {rss_task.exclude}")
                    continue
                # 识别媒体信息
                meta = MetaInfo(title=item.get("title"), subtitle=item.get("description"))
                if not meta.name:
                    logger.error(f"{item.get('title')} 未识别到有效信息")
                    continue
                mediainfo = self.recognize_media(meta=meta)
                if not mediainfo:
                    logger.error(f"{item.get('title')} 未识别到TMDB媒体信息")
                    continue
                if mediainfo.tmdb_id != rss_task.tmdbid:
                    logger.error(f"{item.get('title')} 不匹配")
                    continue
                # 季集是否已处理过
                if meta.season_episode in processed_data.get('season_episodes'):
                    logger.info(f"{meta.season_episode} 已处理过")
                    continue
                # 种子
                torrentinfo = TorrentInfo(
                    site=site_info.get("id"),
                    site_name=site_info.get("name"),
                    site_cookie=site_info.get("cookie"),
                    site_ua=site_info.get("cookie") or settings.USER_AGENT,
                    site_proxy=site_info.get("proxy") or rss_task.proxy,
                    site_order=site_info.get("pri"),
                    title=item.get("title"),
                    description=item.get("description"),
                    enclosure=item.get("enclosure"),
                    page_url=item.get("link"),
                    size=item.get("size"),
                    pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
                )
                # 过滤种子
                if rss_task.filter:
                    result = self.filter_torrents(
                        rule_string=filter_rule,
                        torrent_list=[torrentinfo]
                    )
                    if not result:
                        logger.info(f"{rss_task.name} 不匹配过滤规则")
                        continue
                # 更新已处理数据
                processed_data['titles'].append(item.get("title"))
                processed_data['season_episodes'].append(meta.season_episode)
                # 清除多条数据
                mediainfo.clear()
                # 匹配到的数据
                matched_contexts.append(Context(
                    meta_info=meta,
                    media_info=mediainfo,
                    torrent_info=torrentinfo
                ))
            # 更新已处理过的title
            self.rssoper.update(rssid=rss_task.id, note=json.dumps(processed_data))
            if not matched_contexts:
                logger.info(f"{rss_task.name} 未匹配到数据")
                continue
            logger.info(f"{rss_task.name} 匹配到 {len(matched_contexts)} 条数据")
            # 查询本地存在情况
            if not rss_task.best_version:
                # 查询缺失的媒体信息
                rss_meta = MetaInfo(title=rss_task.title)
                rss_meta.year = rss_task.year
                rss_meta.begin_season = rss_task.season
                rss_meta.type = MediaType(rss_task.type)
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(
                    meta=rss_meta,
                    mediainfo=MediaInfo(
                        title=rss_task.title,
                        year=rss_task.year,
                        tmdb_id=rss_task.tmdbid,
                        season=rss_task.season
                    ),
                )
                if exist_flag:
                    logger.info(f'{rss_task.name} 媒体库中已存在，完成订阅')
                    self.rssoper.delete(rss_task.id)
                    # 发送通知
                    self.post_message(Notification(mtype=NotificationType.Subscribe,
                                                   title=f'自定义订阅 {rss_task.name} 已完成',
                                                   image=rss_task.backdrop))
                    continue
                elif rss_meta.type == MediaType.TV.value:
                    # 打印缺失集信息
                    if no_exists and no_exists.get(rss_task.tmdbid):
                        no_exists_info = no_exists.get(rss_task.tmdbid).get(rss_task.season)
                        if no_exists_info:
                            logger.info(f'订阅 {rss_task.name} 缺失集：{no_exists_info.episodes}')
            else:
                if rss_task.type == MediaType.TV.value:
                    no_exists = {
                        rss_task.season: NotExistMediaInfo(
                            season=rss_task.season,
                            episodes=[],
                            total_episode=rss_task.total_episode,
                            start_episode=1)
                    }
                else:
                    no_exists = {}
            # 开始下载
            downloads, lefts = self.downloadchain.batch_download(contexts=matched_contexts,
                                                                 no_exists=no_exists,
                                                                 save_path=rss_task.save_path)
            if downloads and not lefts:
                if not rss_task.best_version:
                    self.rssoper.delete(rss_task.id)
                    # 发送通知
                    self.post_message(Notification(mtype=NotificationType.Subscribe,
                                                   title=f'自定义订阅 {rss_task.name} 已完成',
                                                   image=rss_task.backdrop))
            # 未完成下载
            logger.info(f'{rss_task.name} 未下载未完整，继续订阅 ...')
            if downloads:
                # 更新最后更新时间和已处理数量
                self.rssoper.update(rssid=rss_task.id,
                                    processed=(rss_task.processed or 0) + len(downloads),
                                    last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("刷新RSS订阅数据完成")
        if manual:
            if len(rss_tasks) == 1:
                self.message.put(f"{rss_tasks[0].name} 自定义订阅刷新完成")
            else:
                self.message.put(f"自定义订阅刷新完成")
