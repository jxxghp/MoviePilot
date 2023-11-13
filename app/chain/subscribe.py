import json
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Union, Tuple

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import NotExistMediaInfo, Notification
from app.schemas.types import MediaType, SystemConfigKey, MessageChannel, NotificationType


class SubscribeChain(ChainBase):
    """
    订阅管理处理链
    """

    def __init__(self):
        super().__init__()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribeoper = SubscribeOper()
        self.torrentschain = TorrentsChain()
        self.mediachain = MediaChain()
        self.message = MessageHelper()
        self.systemconfig = SystemConfigOper()

    def add(self, title: str, year: str,
            mtype: MediaType = None,
            tmdbid: int = None,
            doubanid: str = None,
            season: int = None,
            channel: MessageChannel = None,
            userid: str = None,
            username: str = None,
            message: bool = True,
            exist_ok: bool = False,
            **kwargs) -> Tuple[Optional[int], str]:
        """
        识别媒体信息并添加订阅
        """
        logger.info(f'开始添加订阅，标题：{title} ...')
        mediainfo = None
        metainfo = MetaInfo(title)
        if year:
            metainfo.year = year
        if mtype:
            metainfo.type = mtype
        if season:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # TMDB识别模式
            if not tmdbid and doubanid:
                # 将豆瓣信息转换为TMDB信息
                tmdbinfo = self.mediachain.get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
                if tmdbinfo:
                    mediainfo = MediaInfo(tmdb_info=tmdbinfo)
            else:
                # 识别TMDB信息
                mediainfo = self.recognize_media(meta=metainfo, mtype=mtype, tmdbid=tmdbid)
        else:
            # 豆瓣识别模式
            mediainfo = self.recognize_media(meta=metainfo, mtype=mtype, doubanid=doubanid)
            if mediainfo:
                # 豆瓣标题处理
                meta = MetaInfo(mediainfo.title)
                mediainfo.title = meta.name
                if not season:
                    season = meta.begin_season
        # 识别失败
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}，doubanid：{doubanid}')
            return None, "未识别到媒体信息"
        # 总集数
        if mediainfo.type == MediaType.TV:
            if not season:
                season = 1
            # 总集数
            if not kwargs.get('total_episode'):
                if not mediainfo.seasons:
                    # 补充媒体信息
                    mediainfo = self.recognize_media(mtype=mediainfo.type,
                                                     tmdbid=mediainfo.tmdb_id,
                                                     doubanid=mediainfo.douban_id)
                    if not mediainfo:
                        logger.error(f"媒体信息识别失败！")
                        return None, "媒体信息识别失败"
                    if not mediainfo.seasons:
                        logger.error(f"媒体信息中没有季集信息，标题：{title}，tmdbid：{tmdbid}，doubanid：{doubanid}")
                        return None, "媒体信息中没有季集信息"
                total_episode = len(mediainfo.seasons.get(season) or [])
                if not total_episode:
                    logger.error(f'未获取到总集数，标题：{title}，tmdbid：{tmdbid}, doubanid：{doubanid}')
                    return None, f"未获取到第 {season} 季的总集数"
                kwargs.update({
                    'total_episode': total_episode
                })
            # 缺失集
            if not kwargs.get('lack_episode'):
                kwargs.update({
                    'lack_episode': kwargs.get('total_episode')
                })
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 合并信息
        if doubanid:
            mediainfo.douban_id = doubanid
        # 添加订阅
        sid, err_msg = self.subscribeoper.add(mediainfo, season=season, username=username, **kwargs)
        if not sid:
            logger.error(f'{mediainfo.title_year} {err_msg}')
            if not exist_ok and message:
                # 发回原用户
                self.post_message(Notification(channel=channel,
                                               mtype=NotificationType.Subscribe,
                                               title=f"{mediainfo.title_year} {metainfo.season} "
                                                     f"添加订阅失败！",
                                               text=f"{err_msg}",
                                               image=mediainfo.get_message_image(),
                                               userid=userid))
        elif message:
            logger.info(f'{mediainfo.title_year} {metainfo.season} 添加订阅成功')
            if username or userid:
                text = f"评分：{mediainfo.vote_average}，来自用户：{username or userid}"
            else:
                text = f"评分：{mediainfo.vote_average}"
            # 广而告之
            self.post_message(Notification(channel=channel,
                                           mtype=NotificationType.Subscribe,
                                           title=f"{mediainfo.title_year} {metainfo.season} 已添加订阅",
                                           text=text,
                                           image=mediainfo.get_message_image()))
        # 返回结果
        return sid, ""

    def exists(self, mediainfo: MediaInfo, meta: MetaBase = None):
        """
        判断订阅是否已存在
        """
        if self.subscribeoper.exists(tmdbid=mediainfo.tmdb_id,
                                     doubanid=mediainfo.douban_id,
                                     season=meta.begin_season if meta else None):
            return True
        return False

    def search(self, sid: int = None, state: str = 'N', manual: bool = False):
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:未搜索 R:已搜索
        :param manual: 是否手动搜索
        :return: 更新订阅状态为R或删除订阅
        """
        if sid:
            subscribes = [self.subscribeoper.get(sid)]
        else:
            subscribes = self.subscribeoper.list(state)
        # 遍历订阅
        for subscribe in subscribes:
            mediakey = subscribe.tmdbid or subscribe.doubanid
            # 校验当前时间减订阅创建时间是否大于1分钟，否则跳过先，留出编辑订阅的时间
            if subscribe.date:
                now = datetime.now()
                subscribe_time = datetime.strptime(subscribe.date, '%Y-%m-%d %H:%M:%S')
                if (now - subscribe_time).total_seconds() < 60:
                    logger.debug(f"订阅标题：{subscribe.name} 新增小于1分钟，暂不搜索...")
                    continue
            # 随机休眠1-5分钟
            if not sid and state == 'R':
                sleep_time = random.randint(60, 300)
                logger.info(f'订阅搜索随机休眠 {sleep_time} 秒 ...')
                time.sleep(sleep_time)
            logger.info(f'开始搜索订阅，标题：{subscribe.name} ...')
            # 如果状态为N则更新为R
            if subscribe.state == 'N':
                self.subscribeoper.update(subscribe.id, {'state': 'R'})
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            meta.type = MediaType(subscribe.type)
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                        tmdbid=subscribe.tmdbid,
                                                        doubanid=subscribe.doubanid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                continue

            # 非洗版状态
            if not subscribe.best_version:
                # 每季总集数
                totals = {}
                if subscribe.season and subscribe.total_episode:
                    totals = {
                        subscribe.season: subscribe.total_episode
                    }
                # 查询媒体库缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(
                    meta=meta,
                    mediainfo=mediainfo,
                    totals=totals
                )
            else:
                # 洗版状态
                exist_flag = False
                if meta.type == MediaType.TV:
                    no_exists = {
                        mediakey: {
                            subscribe.season: NotExistMediaInfo(
                                season=subscribe.season,
                                episodes=[],
                                total_episode=subscribe.total_episode,
                                start_episode=subscribe.start_episode or 1)
                        }
                    }
                else:
                    no_exists = {}

            # 已存在
            if exist_flag:
                logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
                continue

            # 电视剧订阅处理缺失集
            if meta.type == MediaType.TV:
                # 使用订阅的总集数和开始集数替换no_exists
                no_exists = self.__get_subscribe_no_exits(
                    no_exists=no_exists,
                    mediakey=mediakey,
                    begin_season=meta.begin_season,
                    total_episode=subscribe.total_episode,
                    start_episode=subscribe.start_episode,

                )
                # 打印缺失集信息
                if no_exists and no_exists.get(mediakey):
                    no_exists_info = no_exists.get(mediakey).get(subscribe.season)
                    if no_exists_info:
                        logger.info(f'订阅 {mediainfo.title_year} {meta.season} 缺失集：{no_exists_info.episodes}')

            # 站点范围
            if subscribe.sites:
                sites = json.loads(subscribe.sites)
            else:
                sites = None

            # 优先级过滤规则
            if subscribe.best_version:
                priority_rule = self.systemconfig.get(SystemConfigKey.BestVersionFilterRules)
            else:
                priority_rule = self.systemconfig.get(SystemConfigKey.SubscribeFilterRules)

            # 过滤规则
            filter_rule = self.get_filter_rule(subscribe)

            # 搜索，同时电视剧会过滤掉不需要的剧集
            contexts = self.searchchain.process(mediainfo=mediainfo,
                                                keyword=subscribe.keyword,
                                                no_exists=no_exists,
                                                sites=sites,
                                                priority_rule=priority_rule,
                                                filter_rule=filter_rule)
            if not contexts:
                logger.warn(f'订阅 {subscribe.keyword or subscribe.name} 未搜索到资源')
                if meta.type == MediaType.TV:
                    # 未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                    self.__update_lack_episodes(lefts=no_exists, subscribe=subscribe,
                                                meta=meta, mediainfo=mediainfo)
                continue

            # 过滤
            matched_contexts = []
            for context in contexts:
                torrent_meta = context.meta_info
                torrent_info = context.torrent_info
                torrent_mediainfo = context.media_info
                # 非洗版
                if not subscribe.best_version:
                    # 如果是电视剧过滤掉已经下载的集数
                    if torrent_mediainfo.type == MediaType.TV:
                        if self.__check_subscribe_note(subscribe, torrent_meta.episode_list):
                            logger.info(f'{torrent_info.title} 对应剧集 {torrent_meta.episode_list} 已下载过')
                            continue
                else:
                    # 洗版时，非整季不要
                    if torrent_mediainfo.type == MediaType.TV:
                        if torrent_meta.episode_list:
                            logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                            continue
                    # 优先级小于已下载优先级的不要
                    if subscribe.current_priority \
                            and torrent_info.pri_order < subscribe.current_priority:
                        logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于已下载优先级')
                        continue
                matched_contexts.append(context)
            if not matched_contexts:
                logger.warn(f'订阅 {subscribe.name} 没有符合过滤条件的资源')
                # 非洗版未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                if meta.type == MediaType.TV and not subscribe.best_version:
                    self.__update_lack_episodes(lefts=no_exists, subscribe=subscribe,
                                                meta=meta, mediainfo=mediainfo)
                continue

            # 自动下载
            downloads, lefts = self.downloadchain.batch_download(contexts=matched_contexts,
                                                                 no_exists=no_exists, username=subscribe.username)
            # 更新已经下载的集数
            if downloads \
                    and meta.type == MediaType.TV \
                    and not subscribe.best_version:
                self.__update_subscribe_note(subscribe=subscribe, downloads=downloads)

            if downloads and not lefts:
                # 判断是否应完成订阅
                self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                             mediainfo=mediainfo, downloads=downloads)
            else:
                # 未完成下载
                logger.info(f'{mediainfo.title_year} 未下载完整，继续订阅 ...')
                if meta.type == MediaType.TV and not subscribe.best_version:
                    # 更新订阅剩余集数和时间
                    update_date = True if downloads else False
                    self.__update_lack_episodes(lefts=lefts, subscribe=subscribe, meta=meta,
                                                mediainfo=mediainfo, update_date=update_date)

        # 手动触发时发送系统消息
        if manual:
            if sid:
                self.message.put(f'订阅 {subscribes[0].name} 搜索完成！')
            else:
                self.message.put('所有订阅搜索完成！')

    def finish_subscribe_or_not(self, subscribe: Subscribe, meta: MetaInfo,
                                mediainfo: MediaInfo, downloads: List[Context] = None):
        """
        判断是否应完成订阅
        """
        if not subscribe.best_version:
            # 全部下载完成
            logger.info(f'{mediainfo.title_year} 完成订阅')
            self.subscribeoper.delete(subscribe.id)
            # 发送通知
            self.post_message(Notification(mtype=NotificationType.Subscribe,
                                           title=f'{mediainfo.title_year} {meta.season} 已完成订阅',
                                           image=mediainfo.get_message_image()))
        elif downloads:
            # 当前下载资源的优先级
            priority = max([item.torrent_info.pri_order for item in downloads])
            if priority == 100:
                logger.info(f'{mediainfo.title_year} 洗版完成，删除订阅')
                self.subscribeoper.delete(subscribe.id)
                # 发送通知
                self.post_message(Notification(mtype=NotificationType.Subscribe,
                                               title=f'{mediainfo.title_year} {meta.season} 已洗版完成',
                                               image=mediainfo.get_message_image()))
            else:
                # 正在洗版，更新资源优先级
                logger.info(f'{mediainfo.title_year} 正在洗版，更新资源优先级')
                self.subscribeoper.update(subscribe.id, {
                    "current_priority": priority
                })

    def refresh(self):
        """
        订阅刷新
        """
        # 触发刷新站点资源，从缓存中匹配订阅
        sites = self.get_subscribed_sites()
        if sites is None:
            return
        self.match(
            self.torrentschain.refresh(sites=sites)
        )

    def get_subscribed_sites(self) -> Optional[List[int]]:
        """
        获取订阅中涉及的所有站点清单（节约资源）
        :return: 返回[]代表所有站点命中，返回None代表没有订阅
        """
        # 查询所有订阅
        subscribes = self.subscribeoper.list('R')
        if not subscribes:
            return None
        ret_sites = []
        # 刷新订阅选中的Rss站点
        for subscribe in subscribes:
            # 如果有一个订阅没有选择站点，则刷新所有订阅站点
            if not subscribe.sites:
                return []
            # 刷新选中的站点
            sub_sites = json.loads(subscribe.sites)
            if sub_sites:
                ret_sites.extend(sub_sites)
        # 去重
        if ret_sites:
            ret_sites = list(set(ret_sites))

        return ret_sites

    def get_filter_rule(self, subscribe: Subscribe):
        """
        获取订阅过滤规则，没有则返回默认规则
        """
        # 默认过滤规则
        if (subscribe.include
                or subscribe.exclude
                or subscribe.quality
                or subscribe.resolution
                or subscribe.effect):
            return {
                "include": subscribe.include,
                "exclude": subscribe.exclude,
                "quality": subscribe.quality,
                "resolution": subscribe.resolution,
                "effect": subscribe.effect,
            }
        # 订阅默认过滤规则
        return self.systemconfig.get(SystemConfigKey.DefaultFilterRules) or {}

    @staticmethod
    def check_filter_rule(torrent_info: TorrentInfo, filter_rule: Dict[str, str]) -> bool:
        """
        检查种子是否匹配订阅过滤规则
        """
        if not filter_rule:
            return True
        # 包含
        include = filter_rule.get("include")
        if include:
            if not re.search(r"%s" % include,
                             f"{torrent_info.title} {torrent_info.description}", re.I):
                logger.info(f"{torrent_info.title} 不匹配包含规则 {include}")
                return False
        # 排除
        exclude = filter_rule.get("exclude")
        if exclude:
            if re.search(r"%s" % exclude,
                         f"{torrent_info.title} {torrent_info.description}", re.I):
                logger.info(f"{torrent_info.title} 匹配排除规则 {exclude}")
                return False
        # 质量
        quality = filter_rule.get("quality")
        if quality:
            if not re.search(r"%s" % quality, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配质量规则 {quality}")
                return False
        # 分辨率
        resolution = filter_rule.get("resolution")
        if resolution:
            if not re.search(r"%s" % resolution, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配分辨率规则 {resolution}")
                return False
        # 特效
        effect = filter_rule.get("effect")
        if effect:
            if not re.search(r"%s" % effect, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配特效规则 {effect}")
                return False
        return True

    def match(self, torrents: Dict[str, List[Context]]):
        """
        从缓存中匹配订阅，并自动下载
        """
        if not torrents:
            logger.warn('没有缓存资源，无法匹配订阅')
            return
        # 所有订阅
        subscribes = self.subscribeoper.list('R')
        # 遍历订阅
        for subscribe in subscribes:
            logger.info(f'开始匹配订阅，标题：{subscribe.name} ...')
            mediakey = subscribe.tmdbid or subscribe.doubanid
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            meta.type = MediaType(subscribe.type)
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                        tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                continue
            # 非洗版
            if not subscribe.best_version:
                # 每季总集数
                totals = {}
                if subscribe.season and subscribe.total_episode:
                    totals = {
                        subscribe.season: subscribe.total_episode
                    }
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(
                    meta=meta,
                    mediainfo=mediainfo,
                    totals=totals
                )
            else:
                # 洗版
                exist_flag = False
                if meta.type == MediaType.TV:
                    no_exists = {
                        mediakey: {
                            subscribe.season: NotExistMediaInfo(
                                season=subscribe.season,
                                episodes=[],
                                total_episode=subscribe.total_episode,
                                start_episode=subscribe.start_episode or 1)
                        }
                    }
                else:
                    no_exists = {}

            # 已存在
            if exist_flag:
                logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
                continue

            # 电视剧订阅
            if meta.type == MediaType.TV:
                # 使用订阅的总集数和开始集数替换no_exists
                no_exists = self.__get_subscribe_no_exits(
                    no_exists=no_exists,
                    mediakey=mediakey,
                    begin_season=meta.begin_season,
                    total_episode=subscribe.total_episode,
                    start_episode=subscribe.start_episode,

                )
                # 打印缺失集信息
                if no_exists and no_exists.get(mediakey):
                    no_exists_info = no_exists.get(mediakey).get(subscribe.season)
                    if no_exists_info:
                        logger.info(f'订阅 {mediainfo.title_year} {meta.season} 缺失集：{no_exists_info.episodes}')

            # 过滤规则
            filter_rule = self.get_filter_rule(subscribe)

            # 遍历缓存种子
            _match_context = []
            for domain, contexts in torrents.items():
                for context in contexts:
                    # 检查是否匹配
                    torrent_meta = context.meta_info
                    torrent_mediainfo = context.media_info
                    torrent_info = context.torrent_info
                    # 比对TMDBID和类型
                    if torrent_mediainfo.tmdb_id != mediainfo.tmdb_id \
                            or torrent_mediainfo.type != mediainfo.type:
                        continue
                    # 优先级过滤规则
                    if subscribe.best_version:
                        priority_rule = self.systemconfig.get(SystemConfigKey.BestVersionFilterRules)
                    else:
                        priority_rule = self.systemconfig.get(SystemConfigKey.SubscribeFilterRules)
                    result: List[TorrentInfo] = self.filter_torrents(
                        rule_string=priority_rule,
                        torrent_list=[torrent_info],
                        mediainfo=torrent_mediainfo)
                    if result is not None and not result:
                        # 不符合过滤规则
                        logger.info(f"{torrent_info.title} 不匹配当前过滤规则")
                        continue
                    # 不在订阅站点范围的不处理
                    if subscribe.sites:
                        sub_sites = json.loads(subscribe.sites)
                        if sub_sites and torrent_info.site not in sub_sites:
                            logger.info(f"{torrent_info.title} 不符合 {torrent_mediainfo.title_year} 订阅站点要求")
                            continue
                    # 如果是电视剧
                    if torrent_mediainfo.type == MediaType.TV:
                        # 有多季的不要
                        if len(torrent_meta.season_list) > 1:
                            logger.info(f'{torrent_info.title} 有多季，不处理')
                            continue
                        # 比对季
                        if torrent_meta.begin_season:
                            if meta.begin_season != torrent_meta.begin_season:
                                logger.info(f'{torrent_info.title} 季不匹配')
                                continue
                        elif meta.begin_season != 1:
                            logger.info(f'{torrent_info.title} 季不匹配')
                            continue
                        # 非洗版
                        if not subscribe.best_version:
                            # 不是缺失的剧集不要
                            if no_exists and no_exists.get(mediakey):
                                # 缺失集
                                no_exists_info = no_exists.get(mediakey).get(subscribe.season)
                                if no_exists_info:
                                    # 是否有交集
                                    if no_exists_info.episodes and \
                                            torrent_meta.episode_list and \
                                            not set(no_exists_info.episodes).intersection(
                                                set(torrent_meta.episode_list)
                                            ):
                                        logger.info(
                                            f'{torrent_info.title} 对应剧集 {torrent_meta.episode_list} 未包含缺失的剧集'
                                        )
                                        continue
                            # 过滤掉已经下载的集数
                            if self.__check_subscribe_note(subscribe, torrent_meta.episode_list):
                                logger.info(f'{torrent_info.title} 对应剧集 {torrent_meta.episode_list} 已下载过')
                                continue
                        else:
                            # 洗版时，非整季不要
                            if meta.type == MediaType.TV:
                                if torrent_meta.episode_list:
                                    logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                                    continue

                    # 过滤规则
                    if not self.check_filter_rule(torrent_info=torrent_info,
                                                  filter_rule=filter_rule):
                        continue

                    # 匹配成功
                    logger.info(f'{mediainfo.title_year} 匹配成功：{torrent_info.title}')
                    _match_context.append(context)

            # 开始下载
            logger.info(f'{mediainfo.title_year} 匹配完成，共匹配到{len(_match_context)}个资源')
            if _match_context:
                # 批量择优下载
                downloads, lefts = self.downloadchain.batch_download(contexts=_match_context, no_exists=no_exists,
                                                                     username=subscribe.username)
                # 更新已经下载的集数
                if downloads and meta.type == MediaType.TV:
                    self.__update_subscribe_note(subscribe=subscribe, downloads=downloads)

                if downloads and not lefts:
                    # 判断是否要完成订阅
                    self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                                 mediainfo=mediainfo, downloads=downloads)
                else:
                    if meta.type == MediaType.TV and not subscribe.best_version:
                        update_date = True if downloads else False
                        # 未完成下载，计算剩余集数
                        self.__update_lack_episodes(lefts=lefts, subscribe=subscribe, meta=meta,
                                                    mediainfo=mediainfo, update_date=update_date)
            else:
                if meta.type == MediaType.TV:
                    # 未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                    self.__update_lack_episodes(lefts=no_exists, subscribe=subscribe,
                                                meta=meta, mediainfo=mediainfo)

    def check(self):
        """
        定时检查订阅，更新订阅信息
        """
        # 查询所有订阅
        subscribes = self.subscribeoper.list()
        if not subscribes:
            # 没有订阅不运行
            return
        # 遍历订阅
        for subscribe in subscribes:
            logger.info(f'开始检查订阅：{subscribe.name} ...')
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            meta.type = MediaType(subscribe.type)
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                        tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                continue
            # 对于电视剧，获取当前季的总集数
            episodes = mediainfo.seasons.get(subscribe.season) or []
            if len(episodes) > (subscribe.total_episode or 0):
                total_episode = len(episodes)
                lack_episode = subscribe.lack_episode + (total_episode - subscribe.total_episode)
                logger.info(
                    f'订阅 {subscribe.name} 总集数变化，更新总集数为{total_episode}，缺失集数为{lack_episode} ...')
            else:
                total_episode = subscribe.total_episode
                lack_episode = subscribe.lack_episode
            # 更新TMDB信息
            self.subscribeoper.update(subscribe.id, {
                "name": mediainfo.title,
                "year": mediainfo.year,
                "vote": mediainfo.vote_average,
                "poster": mediainfo.get_poster_image(),
                "backdrop": mediainfo.get_backdrop_image(),
                "description": mediainfo.overview,
                "imdbid": mediainfo.imdb_id,
                "tvdbid": mediainfo.tvdb_id,
                "total_episode": total_episode,
                "lack_episode": lack_episode
            })
            logger.info(f'订阅 {subscribe.name} 更新完成')

    def __update_subscribe_note(self, subscribe: Subscribe, downloads: List[Context]):
        """
        更新已下载集数到note字段
        """
        # 查询现有Note
        if not downloads:
            return
        note = []
        if subscribe.note:
            note = json.loads(subscribe.note)
        for context in downloads:
            meta = context.meta_info
            mediainfo = context.media_info
            if mediainfo.type != MediaType.TV:
                continue
            if subscribe.tmdbid and mediainfo.tmdb_id \
                    and mediainfo.tmdb_id != subscribe.tmdbid:
                continue
            if subscribe.doubanid and mediainfo.douban_id \
                    and mediainfo.douban_id != subscribe.doubanid:
                continue
            episodes = meta.episode_list
            if not episodes:
                continue
            # 合并已下载集
            note = list(set(note).union(set(episodes)))
            # 更新订阅
            self.subscribeoper.update(subscribe.id, {
                "note": json.dumps(note)
            })

    @staticmethod
    def __check_subscribe_note(subscribe: Subscribe, episodes: List[int]) -> bool:
        """
        检查当前集是否已下载过
        """
        if not subscribe.note:
            return False
        if not episodes:
            return False
        note = json.loads(subscribe.note)
        if set(episodes).issubset(set(note)):
            return True
        return False

    def __update_lack_episodes(self, lefts: Dict[int, Dict[int, NotExistMediaInfo]],
                               subscribe: Subscribe,
                               meta: MetaBase,
                               mediainfo: MediaInfo,
                               update_date: bool = False):
        """
        更新订阅剩余集数
        """
        mediakey = subscribe.tmdbid or subscribe.doubanid
        left_seasons = lefts.get(mediakey)
        if left_seasons:
            for season_info in left_seasons.values():
                season = season_info.season
                if season == subscribe.season:
                    left_episodes = season_info.episodes
                    if not left_episodes:
                        lack_episode = season_info.total_episode
                    else:
                        lack_episode = len(left_episodes)
                    logger.info(f'{mediainfo.title_year} 季 {season} 更新缺失集数为{lack_episode} ...')
                    if update_date:
                        # 同时更新最后时间
                        self.subscribeoper.update(subscribe.id, {
                            "lack_episode": lack_episode,
                            "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                    else:
                        self.subscribeoper.update(subscribe.id, {
                            "lack_episode": lack_episode
                        })
        else:
            # 判断是否应完成订阅
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo)

    def remote_list(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        查询订阅并发送消息
        """
        subscribes = self.subscribeoper.list()
        if not subscribes:
            self.post_message(Notification(channel=channel,
                                           title='没有任何订阅！', userid=userid))
            return
        title = f"共有 {len(subscribes)} 个订阅，回复对应指令操作： " \
                f"\n- 删除订阅：/subscribe_delete [id]" \
                f"\n- 搜索订阅：/subscribe_search [id]" \
                f"\n- 刷新订阅：/subscribe_refresh"
        messages = []
        for subscribe in subscribes:
            if subscribe.type == MediaType.MOVIE.value:
                if subscribe.tmdbid:
                    link = f"https://www.themoviedb.org/movie/{subscribe.tmdbid}"
                else:
                    link = f"https://movie.douban.com/subject/{subscribe.doubanid}"
                messages.append(f"{subscribe.id}. [{subscribe.name}（{subscribe.year}）]({link})")
            else:
                if subscribe.tmdbid:
                    link = f"https://www.themoviedb.org/tv/{subscribe.tmdbid}"
                else:
                    link = f"https://movie.douban.com/subject/{subscribe.doubanid}"
                messages.append(f"{subscribe.id}. [{subscribe.name}（{subscribe.year}）]({link}) "
                                f"第{subscribe.season}季 "
                                f"_{subscribe.total_episode - (subscribe.lack_episode or subscribe.total_episode)}"
                                f"/{subscribe.total_episode}_")
        # 发送列表
        self.post_message(Notification(channel=channel,
                                       title=title, text='\n'.join(messages), userid=userid))

    def remote_delete(self, arg_str: str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        删除订阅
        """
        if not arg_str:
            self.post_message(Notification(channel=channel,
                                           title="请输入正确的命令格式：/subscribe_delete [id]，"
                                                 "[id]为订阅编号", userid=userid))
            return
        arg_strs = str(arg_str).split()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            subscribe_id = int(arg_str)
            subscribe = self.subscribeoper.get(subscribe_id)
            if not subscribe:
                self.post_message(Notification(channel=channel,
                                               title=f"订阅编号 {subscribe_id} 不存在！", userid=userid))
                return
            # 删除订阅
            self.subscribeoper.delete(subscribe_id)
        # 重新发送消息
        self.remote_list(channel, userid)

    @staticmethod
    def __get_subscribe_no_exits(no_exists: Dict[int, Dict[int, NotExistMediaInfo]],
                                 mediakey: Union[str, int],
                                 begin_season: int,
                                 total_episode: int,
                                 start_episode: int):
        """
        根据订阅开始集数和总集数，结合TMDB信息计算当前订阅的缺失集数
        :param no_exists: 缺失季集列表
        :param mediakey: TMDB ID或豆瓣ID
        :param begin_season: 开始季
        :param total_episode: 订阅设定总集数
        :param start_episode: 订阅设定开始集数
        """
        # 使用订阅的总集数和开始集数替换no_exists
        if no_exists \
                and no_exists.get(mediakey) \
                and (total_episode or start_episode):
            # 该季原缺失信息
            no_exist_season = no_exists.get(mediakey).get(begin_season)
            if no_exist_season:
                # 原集列表
                episode_list = no_exist_season.episodes
                # 原总集数
                total = no_exist_season.total_episode
                # 原开始集数
                start = no_exist_season.start_episode

                # 更新剧集列表、开始集数、总集数
                if not episode_list:
                    # 整季缺失
                    episodes = []
                    start_episode = start_episode or start
                    total_episode = total_episode or total
                else:
                    # 部分缺失
                    if not start_episode \
                            and not total_episode:
                        # 无需调整
                        return no_exists
                    if not start_episode:
                        # 没有自定义开始集
                        start_episode = start
                    if not total_episode:
                        # 没有自定义总集数
                        total_episode = total
                    # 新的集列表
                    new_episodes = list(range(max(start_episode, start), total_episode + 1))
                    # 与原集列表取交集
                    episodes = list(set(episode_list).intersection(set(new_episodes)))
                # 更新集合
                no_exists[mediakey][begin_season] = NotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start_episode
                )
        return no_exists
