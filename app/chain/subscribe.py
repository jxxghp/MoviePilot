import copy
import json
import random
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Union, Tuple

from app import schemas
from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings, global_vars
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.event import eventmanager, Event, EventManager
from app.core.meta import MetaBase
from app.core.meta.words import WordsMatcher
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.subscribe import Subscribe
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.helper.subscribe import SubscribeHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import MediaRecognizeConvertEventData
from app.schemas.types import MediaType, SystemConfigKey, MessageChannel, NotificationType, EventType, ChainEventType
from app.utils.singleton import Singleton


class SubscribeChain(ChainBase, metaclass=Singleton):
    """
    订阅管理处理链
    """

    def __init__(self):
        super().__init__()
        self._rlock = threading.RLock()
        self.downloadchain = DownloadChain()
        self.downloadhis = DownloadHistoryOper()
        self.searchchain = SearchChain()
        self.subscribeoper = SubscribeOper()
        self.subscribehelper = SubscribeHelper()
        self.torrentschain = TorrentsChain()
        self.mediachain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.message = MessageHelper()
        self.systemconfig = SystemConfigOper()
        self.torrenthelper = TorrentHelper()
        self.siteoper = SiteOper()

    def add(self, title: str, year: str,
            mtype: MediaType = None,
            tmdbid: Optional[int] = None,
            doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None,
            mediaid: Optional[str] = None,
            episode_group: Optional[str] = None,
            season: Optional[int] = None,
            channel: MessageChannel = None,
            source: Optional[str] = None,
            userid: Optional[str] = None,
            username: Optional[str] = None,
            message: Optional[bool] = True,
            exist_ok: Optional[bool] = False,
            **kwargs) -> Tuple[Optional[int], str]:
        """
        识别媒体信息并添加订阅
        """

        def __get_event_meida(_mediaid: str, _meta: MetaBase) -> Optional[MediaInfo]:
            """
            广播事件解析媒体信息
            """
            event_data = MediaRecognizeConvertEventData(
                mediaid=_mediaid,
                convert_type=settings.RECOGNIZE_SOURCE
            )
            event = eventmanager.send_event(ChainEventType.MediaRecognizeConvert, event_data)
            # 使用事件返回的上下文数据
            if event and event.event_data:
                event_data: MediaRecognizeConvertEventData = event.event_data
                if event_data.media_dict:
                    new_id = event_data.media_dict.get("id")
                    if event_data.convert_type == "themoviedb":
                        return self.mediachain.recognize_media(meta=_meta, tmdbid=new_id)
                    elif event_data.convert_type == "douban":
                        return self.mediachain.recognize_media(meta=_meta, doubanid=new_id)
            return None

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
            if not tmdbid:
                if doubanid:
                    # 将豆瓣信息转换为TMDB信息
                    tmdbinfo = self.mediachain.get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
                    if tmdbinfo:
                        mediainfo = MediaInfo(tmdb_info=tmdbinfo)
                elif mediaid:
                    # 未知前缀，广播事件解析媒体信息
                    mediainfo = __get_event_meida(mediaid, metainfo)
            else:
                # 使用TMDBID识别
                mediainfo = self.recognize_media(meta=metainfo, mtype=mtype, tmdbid=tmdbid,
                                                 episode_group=episode_group, cache=False)
        else:
            if doubanid:
                # 豆瓣识别模式，不使用缓存
                mediainfo = self.recognize_media(meta=metainfo, mtype=mtype, doubanid=doubanid, cache=False)
            elif mediaid:
                # 未知前缀，广播事件解析媒体信息
                mediainfo = __get_event_meida(mediaid, metainfo)
            if mediainfo:
                # 豆瓣标题处理
                meta = MetaInfo(mediainfo.title)
                mediainfo.title = meta.name
                if not season:
                    season = meta.begin_season

        # 使用名称识别兜底
        if not mediainfo:
            mediainfo = self.recognize_media(meta=metainfo, episode_group=episode_group)

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
                if not mediainfo.seasons or episode_group:
                    # 补充媒体信息
                    mediainfo = self.recognize_media(mtype=mediainfo.type,
                                                     tmdbid=mediainfo.tmdb_id,
                                                     doubanid=mediainfo.douban_id,
                                                     bangumiid=mediainfo.bangumi_id,
                                                     episode_group=episode_group,
                                                     cache=False)
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
        else:
            # 避免season为0的问题
            season = None

        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 合并信息
        if doubanid:
            mediainfo.douban_id = doubanid
        if bangumiid:
            mediainfo.bangumi_id = bangumiid

        # 添加订阅
        kwargs.update({
            'quality': self.__get_default_subscribe_config(mediainfo.type, "quality") if not kwargs.get(
                "quality") else kwargs.get("quality"),
            'resolution': self.__get_default_subscribe_config(mediainfo.type, "resolution") if not kwargs.get(
                "resolution") else kwargs.get("resolution"),
            'effect': self.__get_default_subscribe_config(mediainfo.type, "effect") if not kwargs.get(
                "effect") else kwargs.get("effect"),
            'include': self.__get_default_subscribe_config(mediainfo.type, "include") if not kwargs.get(
                "include") else kwargs.get("include"),
            'exclude': self.__get_default_subscribe_config(mediainfo.type, "exclude") if not kwargs.get(
                "exclude") else kwargs.get("exclude"),
            'best_version': self.__get_default_subscribe_config(mediainfo.type, "best_version") if not kwargs.get(
                "best_version") else kwargs.get("best_version"),
            'search_imdbid': self.__get_default_subscribe_config(mediainfo.type, "search_imdbid") if not kwargs.get(
                "search_imdbid") else kwargs.get("search_imdbid"),
            'sites': self.__get_default_subscribe_config(mediainfo.type, "sites") or None if not kwargs.get(
                "sites") else kwargs.get("sites"),
            'downloader': self.__get_default_subscribe_config(mediainfo.type, "downloader") if not kwargs.get(
                "downloader") else kwargs.get("downloader"),
            'save_path': self.__get_default_subscribe_config(mediainfo.type, "save_path") if not kwargs.get(
                "save_path") else kwargs.get("save_path"),
            'filter_groups': self.__get_default_subscribe_config(mediainfo.type, "filter_groups") if not kwargs.get(
                "filter_groups") else kwargs.get("filter_groups")
        })
        sid, err_msg = self.subscribeoper.add(mediainfo=mediainfo, season=season, username=username, **kwargs)
        if not sid:
            logger.error(f'{mediainfo.title_year} {err_msg}')
            if not exist_ok and message:
                # 失败发回原用户
                self.post_message(schemas.Notification(channel=channel,
                                                       source=source,
                                                       mtype=NotificationType.Subscribe,
                                                       title=f"{mediainfo.title_year} {metainfo.season} "
                                                             f"添加订阅失败！",
                                                       text=f"{err_msg}",
                                                       image=mediainfo.get_message_image(),
                                                       userid=userid))
            return None, err_msg
        elif message:
            logger.info(f'{mediainfo.title_year} {metainfo.season} 添加订阅成功')
            if username:
                text = f"评分：{mediainfo.vote_average}，来自用户：{username}"
            else:
                text = f"评分：{mediainfo.vote_average}"
            if mediainfo.type == MediaType.TV:
                link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
            else:
                link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
            # 订阅成功按规则发送消息
            self.post_message(schemas.Notification(mtype=NotificationType.Subscribe,
                                                   title=f"{mediainfo.title_year} {metainfo.season} 已添加订阅",
                                                   text=text,
                                                   image=mediainfo.get_message_image(),
                                                   link=link,
                                                   username=username))
        # 发送事件
        EventManager().send_event(EventType.SubscribeAdded, {
            "subscribe_id": sid,
            "username": username,
            "mediainfo": mediainfo.to_dict(),
        })
        # 统计订阅
        self.subscribehelper.sub_reg_async({
            "name": title,
            "year": year,
            "type": metainfo.type.value,
            "tmdbid": mediainfo.tmdb_id,
            "imdbid": mediainfo.imdb_id,
            "tvdbid": mediainfo.tvdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "season": metainfo.begin_season,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview
        })
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

    def search(self, sid: Optional[int] = None, state: Optional[str] = 'N', manual: Optional[bool] = False):
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:新建, R:订阅中, P:待定, S:暂停
        :param manual: 是否手动搜索
        :return: 更新订阅状态为R或删除订阅
        """
        with self._rlock:
            logger.debug(f"search lock acquired at {datetime.now()}")
            if sid:
                subscribe = self.subscribeoper.get(sid)
                subscribes = [subscribe] if subscribe else []
            else:
                subscribes = self.subscribeoper.list(self.get_states_for_search(state))
            # 遍历订阅
            for subscribe in subscribes:
                if global_vars.is_system_stopped:
                    break
                mediakey = subscribe.tmdbid or subscribe.doubanid
                custom_word_list = subscribe.custom_words.split("\n") if subscribe.custom_words else None
                # 校验当前时间减订阅创建时间是否大于1分钟，否则跳过先，留出编辑订阅的时间
                if subscribe.date:
                    now = datetime.now()
                    subscribe_time = datetime.strptime(subscribe.date, '%Y-%m-%d %H:%M:%S')
                    if (now - subscribe_time).total_seconds() < 60:
                        logger.debug(f"订阅标题：{subscribe.name} 新增小于1分钟，暂不搜索...")
                        continue
                # 随机休眠1-5分钟
                if not sid and state in ['R', 'P']:
                    sleep_time = random.randint(60, 300)
                    logger.info(f'订阅搜索随机休眠 {sleep_time} 秒 ...')
                    time.sleep(sleep_time)
                try:
                    logger.info(f'开始搜索订阅，标题：{subscribe.name} ...')
                    # 生成元数据
                    meta = MetaInfo(subscribe.name)
                    meta.year = subscribe.year
                    meta.begin_season = subscribe.season or None
                    try:
                        meta.type = MediaType(subscribe.type)
                    except ValueError:
                        logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                        continue
                    # 识别媒体信息
                    mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                                tmdbid=subscribe.tmdbid,
                                                                doubanid=subscribe.doubanid,
                                                                episode_group=subscribe.episode_group,
                                                                cache=False)
                    if not mediainfo:
                        logger.warn(
                            f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                        continue

                    # 如果媒体已存在或已下载完毕，跳过当前订阅处理
                    exist_flag, no_exists = self.check_and_handle_existing_media(subscribe=subscribe,
                                                                                 meta=meta,
                                                                                 mediainfo=mediainfo,
                                                                                 mediakey=mediakey)
                    if exist_flag:
                        continue

                    # 站点范围
                    sites = self.get_sub_sites(subscribe)

                    # 优先级过滤规则
                    if subscribe.best_version:
                        rule_groups = subscribe.filter_groups \
                                      or self.systemconfig.get(SystemConfigKey.BestVersionFilterRuleGroups) or []
                    else:
                        rule_groups = subscribe.filter_groups \
                                      or self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups) or []

                    # 搜索，同时电视剧会过滤掉不需要的剧集
                    contexts = self.searchchain.process(mediainfo=mediainfo,
                                                        keyword=subscribe.keyword,
                                                        no_exists=no_exists,
                                                        sites=sites,
                                                        rule_groups=rule_groups,
                                                        area="imdbid" if subscribe.search_imdbid else "title",
                                                        custom_words=custom_word_list,
                                                        filter_params=self.get_params(subscribe))
                    if not contexts:
                        logger.warn(f'订阅 {subscribe.keyword or subscribe.name} 未搜索到资源')
                        self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                                     mediainfo=mediainfo, lefts=no_exists)
                        continue

                    # 过滤搜索结果
                    matched_contexts = []
                    for context in contexts:
                        torrent_meta = context.meta_info
                        torrent_info = context.torrent_info
                        torrent_mediainfo = context.media_info

                        # 洗版
                        if subscribe.best_version:
                            # 洗版时，非整季不要
                            if torrent_mediainfo.type == MediaType.TV:
                                if torrent_meta.episode_list:
                                    logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                                    continue
                            # 洗版时，优先级小于等于已下载优先级的不要
                            if subscribe.current_priority \
                                    and torrent_info.pri_order <= subscribe.current_priority:
                                logger.info(
                                    f'{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级')
                                continue
                        # 更新订阅自定义属性
                        if subscribe.media_category:
                            torrent_mediainfo.category = subscribe.media_category
                        if subscribe.episode_group:
                            torrent_mediainfo.episode_group = subscribe.episode_group
                        matched_contexts.append(context)

                    if not matched_contexts:
                        logger.warn(f'订阅 {subscribe.name} 没有符合过滤条件的资源')
                        self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                                     mediainfo=mediainfo, lefts=no_exists)
                        continue

                    # 自动下载
                    downloads, lefts = self.downloadchain.batch_download(
                        contexts=matched_contexts,
                        no_exists=no_exists,
                        userid=subscribe.username,
                        username=subscribe.username,
                        save_path=subscribe.save_path,
                        downloader=subscribe.downloader,
                        source=self.get_subscribe_source_keyword(subscribe)
                    )

                    # 同步外部修改，更新订阅信息
                    subscribe = self.subscribeoper.get(subscribe.id)

                    # 判断是否应完成订阅
                    if subscribe:
                        self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo,
                                                     downloads=downloads, lefts=lefts)
                finally:
                    # 如果状态为N则更新为R
                    if subscribe and subscribe.state == 'N':
                        self.subscribeoper.update(subscribe.id, {'state': 'R'})

            # 手动触发时发送系统消息
            if manual:
                if subscribes:
                    if sid:
                        self.message.put(f'{subscribes[0].name} 搜索完成！', title="订阅搜索", role="system")
                    else:
                        self.message.put('所有订阅搜索完成！', title="订阅搜索", role="system")
                else:
                    self.message.put('没有找到订阅！', title="订阅搜索", role="system")
            logger.debug(f"search Lock released at {datetime.now()}")

    def update_subscribe_priority(self, subscribe: Subscribe, meta: MetaBase,
                                  mediainfo: MediaInfo, downloads: Optional[List[Context]]):
        """
        更新订阅已下载资源的优先级
        """
        if not downloads:
            return
        if not subscribe.best_version:
            return
        # 当前下载资源的优先级
        priority = max([item.torrent_info.pri_order for item in downloads])
        # 订阅存在待定策略，不管是否已完成，均需更新订阅信息
        self.subscribeoper.update(subscribe.id, {
            "current_priority": priority,
            "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        if priority == 100:
            # 洗版完成
            self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
        else:
            # 正在洗版，更新资源优先级
            logger.info(f'{mediainfo.title_year} 正在洗版，更新资源优先级为 {priority}')

    def finish_subscribe_or_not(self, subscribe: Subscribe, meta: MetaBase, mediainfo: MediaInfo,
                                downloads: List[Context] = None,
                                lefts: Dict[Union[int | str], Dict[int, schemas.NotExistMediaInfo]] = None,
                                force: Optional[bool] = False):
        """
        判断是否应完成订阅
        """
        mediakey = subscribe.tmdbid or subscribe.doubanid
        # 是否有剩余集
        no_lefts = not lefts or not lefts.get(mediakey)
        # 是否完成订阅
        if not subscribe.best_version:
            # 订阅存在待定策略，不管是否已完成，均需更新订阅信息
            # 更新订阅已下载信息
            self.__update_subscribe_note(subscribe=subscribe, downloads=downloads)
            # 更新订阅剩余集数和时间
            self.__update_lack_episodes(lefts=lefts, subscribe=subscribe, mediainfo=mediainfo,
                                        update_date=bool(downloads))
            # 判断是否需要完成订阅
            if ((no_lefts and meta.type == MediaType.TV)
                    or (downloads and meta.type == MediaType.MOVIE)
                    or force):
                self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
            else:
                # 未下载到内容且不完整
                logger.info(f'{mediainfo.title_year} 未下载完整，继续订阅 ...')
        elif downloads:
            # 洗版下载到了内容，更新资源优先级
            self.update_subscribe_priority(subscribe=subscribe, meta=meta,
                                           mediainfo=mediainfo, downloads=downloads)
        elif subscribe.current_priority == 100:
            # 洗版完成
            self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
        else:
            # 洗版，未下载到内容
            logger.info(f'{mediainfo.title_year} 继续洗版 ...')

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

    def get_sub_sites(self, subscribe: Subscribe) -> List[int]:
        """
        获取订阅中涉及的站点清单
        :param subscribe: 订阅信息对象
        :return: 涉及的站点清单
        """
        # 从系统配置获取默认订阅站点
        default_sites = self.systemconfig.get(SystemConfigKey.RssSites) or []
        # 如果订阅未指定站点，直接返回默认站点
        if not subscribe.sites:
            return default_sites
        # 如果默认订阅站点未设置，直接返回订阅指定站点
        if not default_sites:
            return subscribe.sites or []
        # 尝试解析订阅中的站点数据
        user_sites = subscribe.sites
        # 计算 user_sites 和 default_sites 的交集
        intersection_sites = [site for site in user_sites if site in default_sites]
        # 如果交集为空，返回默认站点
        return intersection_sites if intersection_sites else default_sites

    def get_subscribed_sites(self) -> Optional[List[int]]:
        """
        获取订阅中涉及的所有站点清单（节约资源）
        :return: 返回[]代表所有站点命中，返回None代表没有订阅
        """
        # 查询所有订阅
        subscribes = self.subscribeoper.list(self.get_states_for_search('R'))
        if not subscribes:
            return None
        ret_sites = []
        # 刷新订阅选中的Rss站点
        for subscribe in subscribes:
            # 刷新选中的站点
            ret_sites.extend(self.get_sub_sites(subscribe))
        # 去重
        if ret_sites:
            ret_sites = list(set(ret_sites))

        return ret_sites

    def match(self, torrents: Dict[str, List[Context]]):
        """
        从缓存中匹配订阅，并自动下载
        """
        if not torrents:
            logger.warn('没有缓存资源，无法匹配订阅')
            return

        with self._rlock:
            logger.debug(f"match lock acquired at {datetime.now()}")
            # 所有订阅
            subscribes = self.subscribeoper.list(self.get_states_for_search('R'))
            # 遍历订阅
            for subscribe in subscribes:
                if global_vars.is_system_stopped:
                    break
                logger.info(f'开始匹配订阅，标题：{subscribe.name} ...')
                mediakey = subscribe.tmdbid or subscribe.doubanid
                # 生成元数据
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                try:
                    meta.type = MediaType(subscribe.type)
                except ValueError:
                    logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                    continue
                # 订阅的站点域名列表
                domains = []
                if subscribe.sites:
                    domains = self.siteoper.get_domains_by_ids(subscribe.sites)
                # 识别媒体信息
                mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                            tmdbid=subscribe.tmdbid,
                                                            doubanid=subscribe.doubanid,
                                                            episode_group=subscribe.episode_group,
                                                            cache=False)
                if not mediainfo:
                    logger.warn(
                        f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                    continue

                # 如果媒体已存在或已下载完毕，跳过当前订阅处理
                exist_flag, no_exists = self.check_and_handle_existing_media(subscribe=subscribe, meta=meta,
                                                                             mediainfo=mediainfo,
                                                                             mediakey=mediakey)
                if exist_flag:
                    continue

                # 订阅识别词
                if subscribe.custom_words:
                    custom_words_list = subscribe.custom_words.split("\n")
                else:
                    custom_words_list = None

                # 遍历缓存种子
                _match_context = []
                for domain, contexts in torrents.items():
                    if global_vars.is_system_stopped:
                        break
                    if domains and domain not in domains:
                        continue
                    logger.debug(f'开始匹配站点：{domain}，共缓存了 {len(contexts)} 个种子...')
                    for context in contexts:
                        # 提取信息
                        _context = copy.deepcopy(context)
                        torrent_meta = _context.meta_info
                        torrent_mediainfo = _context.media_info
                        torrent_info = _context.torrent_info

                        # 不在订阅站点范围的不处理
                        sub_sites = self.get_sub_sites(subscribe)
                        if sub_sites and torrent_info.site not in sub_sites:
                            logger.debug(f"{torrent_info.site_name} - {torrent_info.title} 不符合订阅站点要求")
                            continue

                        # 有自定义识别词时，需要判断是否需要重新识别
                        if custom_words_list:
                            # 使用org_string，应用一次后理论上不能再次应用
                            _, apply_words = WordsMatcher().prepare(torrent_meta.org_string,
                                                                    custom_words=custom_words_list)
                            if apply_words:
                                logger.info(
                                    f'{torrent_info.site_name} - {torrent_info.title} 因订阅存在自定义识别词，重新识别元数据...')
                                # 重新识别元数据
                                torrent_meta = MetaInfo(title=torrent_info.title, subtitle=torrent_info.description,
                                                        custom_words=custom_words_list)
                                # 更新元数据缓存
                                context.meta_info = torrent_meta
                                # 媒体信息需要重新识别
                                torrent_mediainfo = None

                        # 先判断是否有没识别的种子，否则重新识别
                        if not torrent_mediainfo \
                                or (not torrent_mediainfo.tmdb_id and not torrent_mediainfo.douban_id):
                            # 重新识别媒体信息
                            torrent_mediainfo = self.recognize_media(meta=torrent_meta,
                                                                     episode_group=subscribe.episode_group)
                            if torrent_mediainfo:
                                # 更新种子缓存
                                context.media_info = torrent_mediainfo
                            else:
                                # 通过标题匹配兜底
                                logger.warn(
                                    f'{torrent_info.site_name} - {torrent_info.title} 重新识别失败，尝试通过标题匹配...')
                                if self.torrenthelper.match_torrent(mediainfo=mediainfo,
                                                                    torrent_meta=torrent_meta,
                                                                    torrent=torrent_info):
                                    # 匹配成功
                                    logger.info(
                                        f'{mediainfo.title_year} 通过标题匹配到可选资源：{torrent_info.site_name} - {torrent_info.title}')
                                    torrent_mediainfo = mediainfo
                                    context.media_info = torrent_mediainfo
                                else:
                                    continue

                        # 直接比对媒体信息
                        if torrent_mediainfo and (torrent_mediainfo.tmdb_id or torrent_mediainfo.douban_id):
                            if torrent_mediainfo.type != mediainfo.type:
                                continue
                            if torrent_mediainfo.tmdb_id \
                                    and torrent_mediainfo.tmdb_id != mediainfo.tmdb_id:
                                continue
                            if torrent_mediainfo.douban_id \
                                    and torrent_mediainfo.douban_id != mediainfo.douban_id:
                                continue
                            logger.info(
                                f'{mediainfo.title_year} 通过媒体信ID匹配到可选资源：{torrent_info.site_name} - {torrent_info.title}')
                        else:
                            continue

                        # 如果是电视剧
                        if torrent_mediainfo.type == MediaType.TV:
                            # 有多季的不要
                            if len(torrent_meta.season_list) > 1:
                                logger.debug(f'{torrent_info.title} 有多季，不处理')
                                continue
                            # 比对季
                            if torrent_meta.begin_season:
                                if meta.begin_season != torrent_meta.begin_season:
                                    logger.debug(f'{torrent_info.title} 季不匹配')
                                    continue
                            elif meta.begin_season != 1:
                                logger.debug(f'{torrent_info.title} 季不匹配')
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
                                            logger.debug(
                                                f'{torrent_info.title} 对应剧集 {torrent_meta.episode_list} 未包含缺失的剧集'
                                            )
                                            continue
                            else:
                                # 洗版时，非整季不要
                                if meta.type == MediaType.TV:
                                    if torrent_meta.episode_list:
                                        logger.debug(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                                        continue

                        # 匹配订阅附加参数
                        if not self.torrenthelper.filter_torrent(torrent_info=torrent_info,
                                                                 filter_params=self.get_params(subscribe)):
                            continue

                        # 优先级过滤规则
                        if subscribe.best_version:
                            rule_groups = subscribe.filter_groups \
                                          or self.systemconfig.get(SystemConfigKey.BestVersionFilterRuleGroups)
                        else:
                            rule_groups = subscribe.filter_groups \
                                          or self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
                        result: List[TorrentInfo] = self.filter_torrents(
                            rule_groups=rule_groups,
                            torrent_list=[torrent_info],
                            mediainfo=torrent_mediainfo)
                        if result is not None and not result:
                            # 不符合过滤规则
                            logger.debug(f"{torrent_info.title} 不匹配过滤规则")
                            continue

                        # 洗版时，优先级小于已下载优先级的不要
                        if subscribe.best_version:
                            if subscribe.current_priority \
                                    and torrent_info.pri_order <= subscribe.current_priority:
                                logger.info(
                                    f'{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级')
                                continue

                        # 匹配成功
                        logger.info(f'{mediainfo.title_year} 匹配成功：{torrent_info.title}')
                        # 自定义属性
                        if subscribe.media_category:
                            torrent_mediainfo.category = subscribe.media_category
                        if subscribe.episode_group:
                            torrent_mediainfo.episode_group = subscribe.episode_group
                        _match_context.append(_context)

                if not _match_context:
                    # 未匹配到资源
                    logger.info(f'{mediainfo.title_year} 未匹配到符合条件的资源')
                    self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                                 mediainfo=mediainfo, lefts=no_exists)
                    continue

                # 开始批量择优下载
                logger.info(f'{mediainfo.title_year} 匹配完成，共匹配到{len(_match_context)}个资源')
                downloads, lefts = self.downloadchain.batch_download(contexts=_match_context,
                                                                     no_exists=no_exists,
                                                                     userid=subscribe.username,
                                                                     username=subscribe.username,
                                                                     save_path=subscribe.save_path,
                                                                     downloader=subscribe.downloader,
                                                                     source=self.get_subscribe_source_keyword(subscribe)
                                                                     )

                # 同步外部修改，更新订阅信息
                subscribe = self.subscribeoper.get(subscribe.id)

                # 判断是否要完成订阅
                if subscribe:
                    self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo,
                                                 downloads=downloads, lefts=lefts)
            logger.debug(f"match Lock released at {datetime.now()}")

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
            if global_vars.is_system_stopped:
                break
            logger.info(f'开始更新订阅元数据：{subscribe.name} ...')
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            try:
                meta.type = MediaType(subscribe.type)
            except ValueError:
                logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                continue
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                        tmdbid=subscribe.tmdbid,
                                                        doubanid=subscribe.doubanid,
                                                        episode_group=subscribe.episode_group,
                                                        cache=False)
            if not mediainfo:
                logger.warn(
                    f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                continue
            # 对于电视剧，获取当前季的总集数
            episodes = mediainfo.seasons.get(subscribe.season) or []
            if not subscribe.manual_total_episode and len(episodes):
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
            logger.info(f'{subscribe.name} 订阅元数据更新完成')

    def follow(self):
        """
        刷新follow的用户分享，并自动添加订阅
        """
        follow_users: List[str] = self.systemconfig.get(SystemConfigKey.FollowSubscribers)
        if not follow_users:
            return
        share_subs = self.subscribehelper.get_shares()
        logger.info(f'开始刷新follow用户分享订阅 ...')
        success_count = 0
        for share_sub in share_subs:
            uid = share_sub.get("share_uid")
            if uid and uid in follow_users:
                # 订阅已存在则跳过
                if self.subscribeoper.exists(tmdbid=share_sub.get("tmdbid"),
                                             doubanid=share_sub.get("doubanid"),
                                             season=share_sub.get("season")):
                    continue
                # 已经订阅过跳过
                if self.subscribeoper.exist_history(tmdbid=share_sub.get("tmdbid"),
                                                    doubanid=share_sub.get("doubanid"),
                                                    season=share_sub.get("season")):
                    continue
                # 去除无效属性
                for key in list(share_sub.keys()):
                    if not hasattr(schemas.Subscribe(), key):
                        share_sub.pop(key)
                # 类型转换
                subscribe_in = schemas.Subscribe(**share_sub)
                mtype = MediaType(subscribe_in.type)
                # 豆瓣标题处理
                if subscribe_in.doubanid or subscribe_in.bangumiid:
                    meta = MetaInfo(subscribe_in.name)
                    subscribe_in.name = meta.name
                    subscribe_in.season = meta.begin_season
                # 标题转换
                if subscribe_in.name:
                    title = subscribe_in.name
                else:
                    title = None
                sid, message = SubscribeChain().add(mtype=mtype,
                                                    title=title,
                                                    year=subscribe_in.year,
                                                    tmdbid=subscribe_in.tmdbid,
                                                    season=subscribe_in.season,
                                                    doubanid=subscribe_in.doubanid,
                                                    bangumiid=subscribe_in.bangumiid,
                                                    username="订阅分享",
                                                    best_version=subscribe_in.best_version,
                                                    save_path=subscribe_in.save_path,
                                                    search_imdbid=subscribe_in.search_imdbid,
                                                    custom_words=subscribe_in.custom_words,
                                                    media_category=subscribe_in.media_category,
                                                    filter_groups=subscribe_in.filter_groups,
                                                    exist_ok=True)
                if sid:
                    success_count += 1
                    logger.info(f'follow用户分享订阅 {title} 添加成功')
                else:
                    logger.error(f'follow用户分享订阅 {title} 添加失败：{message}')
        logger.info(f'follow用户分享订阅刷新完成，共添加 {success_count} 个订阅')

    def __update_subscribe_note(self, subscribe: Subscribe, downloads: Optional[List[Context]]):
        """
        更新已下载信息到note字段
        """
        # 查询现有Note
        if not downloads:
            return
        note = []
        if subscribe.note:
            note = subscribe.note or []
        for context in downloads:
            meta = context.meta_info
            mediainfo = context.media_info
            if subscribe.tmdbid and mediainfo.tmdb_id \
                    and mediainfo.tmdb_id != subscribe.tmdbid:
                continue
            if subscribe.doubanid and mediainfo.douban_id \
                    and mediainfo.douban_id != subscribe.doubanid:
                continue
            items = []
            if mediainfo.type == MediaType.TV:
                # 电视剧有集数，使用 episode_list
                items = meta.episode_list
            elif mediainfo.type == MediaType.MOVIE:
                # 电影只有一个条目，设置为 [1]
                items = [1]
            if not items:
                continue
            # 合并已下载的集数或电影项（去重）
            note = list(set(note).union(set(items)))
        # 更新订阅
        if note:
            self.subscribeoper.update(subscribe.id, {
                "note": note
            })

    @staticmethod
    def __get_downloaded(subscribe: Subscribe) -> List[int]:
        """
        获取已下载过的集数或电影
        """
        if subscribe.best_version:
            return []
        note = subscribe.note or []
        if not note:
            return []
        # 针对 TV 类型，返回已下载的集数
        if subscribe.type == MediaType.TV.value:
            logger.info(f'订阅 {subscribe.name} 第{subscribe.season}季 已下载集数：{note}')
            return note
        # 针对 Movie 类型，直接返回已下载的电影
        if subscribe.type == MediaType.MOVIE.value:
            logger.info(f'订阅 {subscribe.name} 已下载内容：{note}')
            return note
        return []

    def __update_lack_episodes(self, lefts: Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]],
                               subscribe: Subscribe,
                               mediainfo: MediaInfo,
                               update_date: Optional[bool] = False):
        """
        更新订阅剩余集数及时间
        """
        update_data = {}
        if update_date:
            update_data["last_update"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if subscribe.type == MediaType.TV.value:
            if not lefts:
                # 如果 lefts 为空，表示没有缺失集数，直接设置 lack_episode 为 0
                lack_episode = 0
                logger.info(f'{mediainfo.title_year} 没有缺失集数，直接更新为 0 ...')
            else:
                mediakey = subscribe.tmdbid or subscribe.doubanid
                left_seasons = lefts.get(mediakey)
                lack_episode = 0
                if left_seasons:
                    for season_info in left_seasons.values():
                        season = season_info.season
                        if season == subscribe.season:
                            left_episodes = season_info.episodes
                            if not left_episodes:
                                lack_episode = season_info.total_episode
                            else:
                                lack_episode = len(left_episodes)
                            logger.info(f"{mediainfo.title_year} 季 {season} 更新缺失集数为{lack_episode} ...")
                            break
            update_data["lack_episode"] = lack_episode
        # 更新数据库
        if update_data:
            self.subscribeoper.update(subscribe.id, update_data)

    def __finish_subscribe(self, subscribe: Subscribe, mediainfo: MediaInfo, meta: MetaBase):
        """
        完成订阅
        """
        # 如果订阅状态为待定（P），说明订阅信息尚未完全更新，无法完成订阅
        if subscribe.state == "P":
            return
        # 完成订阅
        msgstr = "订阅" if not subscribe.best_version else "洗版"
        logger.info(f'{mediainfo.title_year} 完成{msgstr}')
        # 新增订阅历史
        self.subscribeoper.add_history(**subscribe.to_dict())
        # 删除订阅
        self.subscribeoper.delete(subscribe.id)
        # 发送通知
        if mediainfo.type == MediaType.TV:
            link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
        else:
            link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
        # 完成订阅按规则发送消息
        self.post_message(schemas.Notification(mtype=NotificationType.Subscribe,
                                               title=f'{mediainfo.title_year} {meta.season} 已完成{msgstr}',
                                               image=mediainfo.get_message_image(),
                                               link=link,
                                               username=subscribe.username))
        # 发送事件
        EventManager().send_event(EventType.SubscribeComplete, {
            "subscribe_id": subscribe.id,
            "subscribe_info": subscribe.to_dict(),
            "mediainfo": mediainfo.to_dict(),
        })
        # 统计订阅
        self.subscribehelper.sub_done_async({
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id
        })

    def remote_list(self, channel: MessageChannel,
                    userid: Union[str, int] = None, source: Optional[str] = None):
        """
        查询订阅并发送消息
        """
        subscribes = self.subscribeoper.list()
        if not subscribes:
            self.post_message(schemas.Notification(channel=channel,
                                                   source=source,
                                                   title='没有任何订阅！', userid=userid))
            return
        title = f"共有 {len(subscribes)} 个订阅，回复对应指令操作： " \
                f"\n- 删除订阅：/subscribe_delete [id]" \
                f"\n- 搜索订阅：/subscribe_search [id]" \
                f"\n- 刷新订阅：/subscribe_refresh"
        messages = []
        for subscribe in subscribes:
            if subscribe.type == MediaType.MOVIE.value:
                messages.append(f"{subscribe.id}. {subscribe.name}（{subscribe.year}）")
            else:
                messages.append(f"{subscribe.id}. {subscribe.name}（{subscribe.year}）"
                                f"第{subscribe.season}季 "
                                f"[{subscribe.total_episode - (subscribe.lack_episode or subscribe.total_episode)}"
                                f"/{subscribe.total_episode}]")
        # 发送列表
        self.post_message(schemas.Notification(channel=channel, source=source,
                                               title=title, text='\n'.join(messages), userid=userid))

    def remote_delete(self, arg_str: str, channel: MessageChannel,
                      userid: Union[str, int] = None, source: Optional[str] = None):
        """
        删除订阅
        """
        if not arg_str:
            self.post_message(schemas.Notification(channel=channel, source=source,
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
                self.post_message(schemas.Notification(channel=channel, source=source,
                                                       title=f"订阅编号 {subscribe_id} 不存在！", userid=userid))
                return
            # 删除订阅
            self.subscribeoper.delete(subscribe_id)
            # 统计订阅
            self.subscribehelper.sub_done_async({
                "tmdbid": subscribe.tmdbid,
                "doubanid": subscribe.doubanid
            })
        # 重新发送消息
        self.remote_list(channel=channel, userid=userid, source=source)

    @staticmethod
    def __get_subscribe_no_exits(subscribe_name: str,
                                 no_exists: Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]],
                                 mediakey: Union[str, int],
                                 begin_season: int,
                                 total_episode: Optional[int],
                                 start_episode: Optional[int],
                                 downloaded_episodes: List[int] = None
                                 ) -> Tuple[bool, Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]]]:
        """
        根据订阅开始集数和总集数，结合TMDB信息计算当前订阅的缺失集数
        :param subscribe_name: 订阅名称
        :param no_exists: 缺失季集列表
        :param mediakey: TMDB ID或豆瓣ID
        :param begin_season: 开始季
        :param total_episode: 订阅设定总集数
        :param start_episode: 订阅设定开始集数
        :param downloaded_episodes: 已下载集数
        """
        # 使用订阅的总集数和开始集数替换no_exists
        if not no_exists or not no_exists.get(mediakey):
            return False, no_exists
        no_exists_item = no_exists.get(mediakey)
        if total_episode or start_episode:
            logger.info(f'订阅 {subscribe_name} 设定的开始集数：{start_episode}、总集数：{total_episode}')
            # 该季原缺失信息
            no_exist_season = no_exists_item.get(begin_season)
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
                        return False, no_exists
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
                no_exists[mediakey][begin_season] = schemas.NotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start_episode
                )
        # 根据订阅已下载集数更新缺失集数
        if downloaded_episodes:
            logger.info(f'订阅 {subscribe_name} 已下载集数：{downloaded_episodes}')
            # 该季原缺失信息
            no_exist_season = no_exists_item.get(begin_season)
            if no_exist_season:
                # 原集列表
                episode_list = no_exist_season.episodes
                # 原总集数
                total = no_exist_season.total_episode
                # 原开始集数
                start = no_exist_season.start_episode
                # 整季缺失
                if not episode_list:
                    episode_list = list(range(start, total + 1))
                # 更新剧集列表
                episodes = list(set(episode_list).difference(set(downloaded_episodes)))
                # 如果存在已下载剧集，则差集为空时，说明所有均已存在
                if not episodes:
                    return True, {}
                # 更新集合
                no_exists[mediakey][begin_season] = schemas.NotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total,
                    start_episode=start,
                )
            else:
                # 开始集数
                start = start_episode or 1
                # 更新剧集列表
                episodes = list(set(range(start, total_episode + 1)).difference(set(downloaded_episodes)))
                # 如果存在已下载剧集，则差集为空时，说明所有均已存在
                if not episodes:
                    return True, {}
                no_exists[mediakey][begin_season] = schemas.NotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start,
                )
        logger.info(f'订阅 {subscribe_name} 缺失剧集数更新为：{no_exists}')
        return False, no_exists

    @eventmanager.register(EventType.SiteDeleted)
    def remove_site(self, event: Event):
        """
        从订阅中移除与站点相关的设置
        """
        if not event:
            return
        event_data = event.event_data or {}
        site_id = event_data.get("site_id")
        if not site_id:
            return
        if site_id == "*":
            # 站点被重置
            SystemConfigOper().set(SystemConfigKey.RssSites, [])
            for subscribe in self.subscribeoper.list():
                if not subscribe.sites:
                    continue
                self.subscribeoper.update(subscribe.id, {
                    "sites": []
                })
            return
        # 从选中的rss站点中移除
        selected_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []
        if site_id in selected_sites:
            selected_sites.remove(site_id)
            SystemConfigOper().set(SystemConfigKey.RssSites, selected_sites)
        # 查询所有订阅
        for subscribe in self.subscribeoper.list():
            if not subscribe.sites:
                continue
            sites = subscribe.sites or []
            if site_id not in sites:
                continue
            sites.remove(site_id)
            self.subscribeoper.update(subscribe.id, {
                "sites": sites
            })

    @staticmethod
    def __get_default_subscribe_config(mtype: MediaType, default_config_key: str) -> Optional[str]:
        """
        获取默认订阅配置
        """
        default_subscribe_key = None
        if mtype == MediaType.TV:
            default_subscribe_key = SystemConfigKey.DefaultTvSubscribeConfig.value
        if mtype == MediaType.MOVIE:
            default_subscribe_key = SystemConfigKey.DefaultMovieSubscribeConfig.value

        # 默认订阅规则
        if hasattr(settings, default_subscribe_key):
            value = getattr(settings, default_subscribe_key)
        else:
            value = SystemConfigOper().get(default_subscribe_key)

        if not value:
            return None
        return value.get(default_config_key) or None

    def get_params(self, subscribe: Subscribe):
        """
        获取订阅默认参数
        """
        # 默认过滤规则
        default_rule = self.systemconfig.get(SystemConfigKey.SubscribeDefaultParams) or {}
        return {
            key: value for key, value in {
                "include": subscribe.include or default_rule.get("include"),
                "exclude": subscribe.exclude or default_rule.get("exclude"),
                "quality": subscribe.quality or default_rule.get("quality"),
                "resolution": subscribe.resolution or default_rule.get("resolution"),
                "effect": subscribe.effect or default_rule.get("effect"),
                "tv_size": default_rule.get("tv_size"),
                "movie_size": default_rule.get("movie_size"),
                "min_seeders": default_rule.get("min_seeders"),
                "min_seeders_time": default_rule.get("min_seeders_time"),
            }.items() if value is not None}

    def subscribe_files_info(self, subscribe: Subscribe) -> Optional[schemas.SubscrbieInfo]:
        """
        订阅相关的下载和文件信息
        """
        if not subscribe:
            return None

        # 返回订阅数据
        subscribe_info = schemas.SubscrbieInfo()

        # 所有集的数据
        episodes: Dict[int, schemas.SubscribeEpisodeInfo] = {}
        if subscribe.tmdbid and subscribe.type == MediaType.TV.value:
            # 查询TMDB中的集信息
            tmdb_episodes = self.tmdbchain.tmdb_episodes(
                tmdbid=subscribe.tmdbid,
                season=subscribe.season,
                episode_group=subscribe.episode_group
            )
            if tmdb_episodes:
                for episode in tmdb_episodes:
                    info = schemas.SubscribeEpisodeInfo()
                    info.title = episode.name
                    info.description = episode.overview
                    info.backdrop = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/w500${episode.still_path}"
                    episodes[episode.episode_number] = info
        elif subscribe.type == MediaType.TV.value:
            # 根据开始结束集计算集信息
            for i in range(subscribe.start_episode or 1, subscribe.total_episode + 1):
                info = schemas.SubscribeEpisodeInfo()
                info.title = f'第 {i} 集'
                episodes[i] = info
        else:
            # 电影
            info = schemas.SubscribeEpisodeInfo()
            info.title = subscribe.name
            episodes[0] = info

        # 所有下载记录
        download_his = self.downloadhis.get_by_mediaid(tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid)
        if download_his:
            for his in download_his:
                # 查询下载文件
                files = self.downloadhis.get_files_by_hash(his.download_hash)
                if files:
                    for file in files:
                        # 识别文件名
                        file_meta = MetaInfo(file.filepath)
                        # 下载文件信息
                        file_info = schemas.SubscribeDownloadFileInfo(
                            torrent_title=his.torrent_name,
                            site_name=his.torrent_site,
                            downloader=file.downloader,
                            hash=his.download_hash,
                            file_path=file.fullpath,
                        )
                        if subscribe.type == MediaType.TV.value:
                            season_number = file_meta.begin_season
                            if season_number and season_number != subscribe.season:
                                continue
                            episode_number = file_meta.begin_episode
                            if episode_number and episodes.get(episode_number):
                                episodes[episode_number].download.append(file_info)
                        else:
                            episodes[0].download.append(file_info)

        # 生成元数据
        meta = MetaInfo(subscribe.name)
        meta.year = subscribe.year
        meta.begin_season = subscribe.season or None
        try:
            meta.type = MediaType(subscribe.type)
        except ValueError:
            logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
            return subscribe_info
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type,
                                                    tmdbid=subscribe.tmdbid,
                                                    doubanid=subscribe.doubanid,
                                                    episode_group=subscribe.episode_group,
                                                    cache=False)
        if not mediainfo:
            logger.warn(
                f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
            return subscribe_info

        # 所有媒体库文件记录
        library_fileitems = self.media_files(mediainfo)
        if library_fileitems:
            for fileitem in library_fileitems:
                # 识别文件名
                file_meta = MetaInfo(fileitem.path)
                # 媒体库文件信息
                file_info = schemas.SubscribeLibraryFileInfo(
                    storage=fileitem.storage,
                    file_path=fileitem.path,
                )
                if subscribe.type == MediaType.TV.value:
                    season_number = file_meta.begin_season
                    if season_number and season_number != subscribe.season:
                        continue
                    episode_number = file_meta.begin_episode
                    if episode_number and episodes.get(episode_number):
                        episodes[episode_number].library.append(file_info)
                else:
                    episodes[0].library.append(file_info)

        # 更新订阅信息
        subscribe_info.subscribe = Subscribe(**subscribe.to_dict())
        subscribe_info.episodes = episodes
        return subscribe_info

    def check_and_handle_existing_media(self, subscribe: Subscribe, meta: MetaBase,
                                        mediainfo: MediaInfo, mediakey: Union[str, int]):
        """
        检查媒体是否已经存在，并根据情况执行相应的操作
        1. 查询缺失的媒体信息
        2. 判断是否已经下载完毕
        3. 根据媒体类型（电视剧或电影）执行不同的处理

        :param subscribe: 订阅信息对象
        :param meta: 媒体元数据
        :param mediainfo: 媒体信息
        :param mediakey: 媒体标识符
        :return:
            - exist_flag (bool): 布尔值，表示媒体是否已经完全下载或已存在
            - no_exists (dict): 缺失的媒体信息，包含缺失的集数或其他相关信息
        """
        # 非洗版
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
            # 洗版，如果已经满足了优先级，则认为已经洗版完成
            if subscribe.current_priority == 100:
                exist_flag = True
                no_exists = {}
            else:
                exist_flag = False
                if meta.type == MediaType.TV:
                    # 对于电视剧，构造缺失的媒体信息
                    no_exists = {
                        mediakey: {
                            subscribe.season: schemas.NotExistMediaInfo(
                                season=subscribe.season,
                                episodes=[],
                                total_episode=subscribe.total_episode,
                                start_episode=subscribe.start_episode or 1)
                        }
                    }
                else:
                    no_exists = {}

        # 如果媒体已存在，执行订阅完成操作
        if exist_flag:
            if not subscribe.best_version:
                logger.info(f'{mediainfo.title_year} 媒体库中已存在')
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, force=True)
            return True, no_exists

        # 获取已下载的集数或电影
        downloaded = self.__get_downloaded(subscribe)
        if meta.type == MediaType.TV:
            # 对于电视剧类型，整合缺失集数并剔除已下载的集数
            exist_flag, no_exists = self.__get_subscribe_no_exits(
                subscribe_name=f'{subscribe.name} {meta.season}',
                no_exists=no_exists,
                mediakey=mediakey,
                begin_season=meta.begin_season,
                total_episode=subscribe.total_episode,
                start_episode=subscribe.start_episode,
                downloaded_episodes=downloaded
            )
        elif meta.type == MediaType.MOVIE:
            # 对于电影类型，直接根据是否已下载判断
            exist_flag = bool(downloaded)

        # 如果已下载完毕，执行订阅完成操作
        if exist_flag:
            logger.info(f'{mediainfo.title_year} 已全部下载')
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, force=True)
            return True, no_exists

        # 返回结果，表示媒体未完全下载或存在
        return False, no_exists

    @staticmethod
    def get_states_for_search(state: str) -> str:
        """
        根据给定的状态返回实际需要搜索的状态列表，支持多个状态用逗号分隔
        :param state: 订阅状态
            N: New（新建，未处理）
            R: Resolved（订阅中）
            P: Pending（待定，信息待进一步更新，允许搜索，不允许完成）
            S: Suspended（暂停，订阅不参与任何动作，暂时停止处理）
        :return: 需要查询的状态列表（多个状态用逗号分隔）
        """
        # 如果状态是 R 或 P，则视为一起搜索，返回 R,P 作为查询条件
        if state in ["R", "P"]:
            return "R,P"
        return state

    @staticmethod
    def get_subscribe_source_keyword(subscribe: Subscribe) -> str:
        """
        构造用于订阅来源的关键字字符串
        :param subscribe: Subscribe 对象
        :return: 格式化的订阅来源关键字字符串，格式为 "Subscribe|{...}"
        """
        source_keyword = {
            'id': subscribe.id,
            'name': subscribe.name,
            'year': subscribe.year,
            'type': subscribe.type,
            'season': subscribe.season,
            'tmdbid': subscribe.tmdbid,
            'imdbid': subscribe.imdbid,
            'tvdbid': subscribe.tvdbid,
            'doubanid': subscribe.doubanid,
            'bangumiid': subscribe.bangumiid
        }
        return f"Subscribe|{json.dumps(source_keyword, ensure_ascii=False)}"
