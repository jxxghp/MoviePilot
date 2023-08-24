import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Union, Tuple

from requests import Session

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import NotExistMediaInfo, Notification
from app.schemas.types import MediaType, SystemConfigKey, MessageChannel, NotificationType
from app.utils.string import StringUtils


class SubscribeChain(ChainBase):
    """
    订阅管理处理链
    """

    _cache_file = "__torrents_cache__"

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.downloadchain = DownloadChain(self._db)
        self.searchchain = SearchChain(self._db)
        self.subscribeoper = SubscribeOper(self._db)
        self.siteshelper = SitesHelper()
        self.message = MessageHelper()
        self.systemconfig = SystemConfigOper(self._db)

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
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo, mtype=mtype, tmdbid=tmdbid)
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}')
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
                        logger.error(f"媒体信息中没有季集信息，标题：{title}，tmdbid：{tmdbid}")
                        return None, "媒体信息中没有季集信息"
                total_episode = len(mediainfo.seasons.get(season) or [])
                if not total_episode:
                    logger.error(f'未获取到总集数，标题：{title}，tmdbid：{tmdbid}')
                    return None, "未获取到总集数"
                kwargs.update({
                    'total_episode': total_episode
                })
            # 缺失集
            if not kwargs.get('lack_episode'):
                kwargs.update({
                    'lack_episode': kwargs.get('total_episode')
                })
        # 添加订阅
        sid, err_msg = self.subscribeoper.add(mediainfo, doubanid=doubanid,
                                              season=season, username=username, **kwargs)
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
                                     season=meta.begin_season if meta else None):
            return True
        return False

    def remote_refresh(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程刷新订阅，发送消息
        """
        self.post_message(Notification(channel=channel,
                                       title=f"开始刷新订阅 ...", userid=userid))
        self.refresh()
        self.post_message(Notification(channel=channel,
                                       title=f"订阅刷新完成！", userid=userid))

    def remote_search(self, arg_str: str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程搜索订阅，发送消息
        """
        if arg_str and not str(arg_str).isdigit():
            self.post_message(Notification(channel=channel,
                                           title="请输入正确的命令格式：/subscribe_search [id]，"
                                                 "[id]为订阅编号，不输入订阅编号时搜索所有订阅", userid=userid))
            return
        if arg_str:
            sid = int(arg_str)
            subscribe = self.subscribeoper.get(sid)
            if not subscribe:
                self.post_message(Notification(channel=channel,
                                               title=f"订阅编号 {sid} 不存在！", userid=userid))
                return
            self.post_message(Notification(channel=channel,
                                           title=f"开始搜索 {subscribe.name} ...", userid=userid))
            # 搜索订阅
            self.search(sid=int(arg_str))
            self.post_message(Notification(channel=channel,
                                           title=f"{subscribe.name} 搜索完成！", userid=userid))
        else:
            self.post_message(Notification(channel=channel,
                                           title=f"开始搜索所有订阅 ...", userid=userid))
            self.search(state='R')
            self.post_message(Notification(channel=channel,
                                           title=f"订阅搜索完成！", userid=userid))

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
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue

            # 非洗版状态
            if not subscribe.best_version:
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在，完成订阅')
                    self.subscribeoper.delete(subscribe.id)
                    # 发送通知
                    self.post_message(Notification(mtype=NotificationType.Subscribe,
                                                   title=f'{mediainfo.title_year} {meta.season} 已完成订阅',
                                                   image=mediainfo.get_message_image()))
                    continue
                # 电视剧订阅
                if meta.type == MediaType.TV:
                    # 使用订阅的总集数和开始集数替换no_exists
                    no_exists = self.__get_subscribe_no_exits(
                        no_exists=no_exists,
                        tmdb_id=mediainfo.tmdb_id,
                        begin_season=meta.begin_season,
                        total_episode=subscribe.total_episode,
                        start_episode=subscribe.start_episode,

                    )
                    # 打印缺失集信息
                    if no_exists and no_exists.get(subscribe.tmdbid):
                        no_exists_info = no_exists.get(subscribe.tmdbid).get(subscribe.season)
                        if no_exists_info:
                            logger.info(f'订阅 {mediainfo.title_year} {meta.season} 缺失集：{no_exists_info.episodes}')
            else:
                # 洗版状态
                if meta.type == MediaType.TV:
                    no_exists = {
                        subscribe.season: NotExistMediaInfo(
                            season=subscribe.season,
                            episodes=[],
                            total_episode=subscribe.total_episode,
                            start_episode=subscribe.start_episode or 1)
                    }
                else:
                    no_exists = {}
            # 站点范围
            if subscribe.sites:
                sites = json.loads(subscribe.sites)
            else:
                sites = None
            # 过滤规则
            if subscribe.best_version:
                filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules2)
            else:
                filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules)
            # 搜索，同时电视剧会过滤掉不需要的剧集
            contexts = self.searchchain.process(mediainfo=mediainfo,
                                                keyword=subscribe.keyword,
                                                no_exists=no_exists,
                                                sites=sites,
                                                filter_rule=filter_rule)
            if not contexts:
                logger.warn(f'订阅 {subscribe.keyword or subscribe.name} 未搜索到资源')
                if meta.type == MediaType.TV:
                    # 未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                    self.__upate_lack_episodes(lefts=no_exists, subscribe=subscribe, mediainfo=mediainfo)
                continue
            # 过滤
            matched_contexts = []
            for context in contexts:
                torrent_meta = context.meta_info
                torrent_info = context.torrent_info
                torrent_mediainfo = context.media_info
                # 包含
                if subscribe.include:
                    if not re.search(r"%s" % subscribe.include,
                                     f"{torrent_info.title} {torrent_info.description}", re.I):
                        continue
                # 排除
                if subscribe.exclude:
                    if re.search(r"%s" % subscribe.exclude,
                                 f"{torrent_info.title} {torrent_info.description}", re.I):
                        continue
                # 非洗版
                if not subscribe.best_version:
                    # 如果是电视剧过滤掉已经下载的集数
                    if torrent_mediainfo.type == MediaType.TV:
                        if self.__check_subscribe_note(subscribe, torrent_meta.episode_list):
                            logger.info(f'{torrent_info.title} 对应剧集 {torrent_meta.episodes} 已下载过')
                            continue
                else:
                    # 洗版时，非整季不要
                    if torrent_mediainfo.type == MediaType.TV:
                        if torrent_meta.episode_list:
                            logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                            continue
                matched_contexts.append(context)
            if not matched_contexts:
                logger.warn(f'订阅 {subscribe.name} 没有符合过滤条件的资源')
                # 非洗版未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                if meta.type == MediaType.TV and not subscribe.best_version:
                    self.__upate_lack_episodes(lefts=no_exists, subscribe=subscribe, mediainfo=mediainfo)
                continue
            # 自动下载
            downloads, lefts = self.downloadchain.batch_download(contexts=matched_contexts,
                                                                 no_exists=no_exists)
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
                logger.info(f'{mediainfo.title_year} 未下载未完整，继续订阅 ...')
                if meta.type == MediaType.TV and not subscribe.best_version:
                    # 更新订阅剩余集数和时间
                    update_date = True if downloads else False
                    self.__upate_lack_episodes(lefts=lefts, subscribe=subscribe,
                                               mediainfo=mediainfo, update_date=update_date)
        # 手动触发时发送系统消息
        if manual:
            if sid:
                self.message.put(f'订阅 {subscribes[0].name} 搜索完成！')
            else:
                self.message.put(f'所有订阅搜索完成！')

    def finish_subscribe_or_not(self, subscribe: Subscribe, meta: MetaInfo,
                                mediainfo: MediaInfo, downloads: List[Context]):
        """
        判断是否应完成订阅
        """
        if not subscribe.best_version:
            # 全部下载完成
            logger.info(f'{mediainfo.title_year} 下载完成，完成订阅')
            self.subscribeoper.delete(subscribe.id)
            # 发送通知
            self.post_message(Notification(mtype=NotificationType.Subscribe,
                                           title=f'{mediainfo.title_year} {meta.season} 已完成订阅',
                                           image=mediainfo.get_message_image()))
        else:
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
        刷新站点最新资源
        """
        # 所有订阅
        subscribes = self.subscribeoper.list('R')
        if not subscribes:
            # 没有订阅不运行
            return
        # 读取缓存
        torrents_cache: Dict[str, List[Context]] = self.load_cache(self._cache_file) or {}

        # 所有站点索引
        indexers = self.siteshelper.get_indexers()
        # 配置的索引站点
        config_indexers = [str(sid) for sid in self.systemconfig.get(SystemConfigKey.IndexerSites) or []]
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
        # 从缓存中匹配订阅
        self.match(torrents_cache)
        # 保存缓存到本地
        self.save_cache(torrents_cache, self._cache_file)

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
            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            meta.type = MediaType(subscribe.type)
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type, tmdbid=subscribe.tmdbid)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}')
                continue
            # 非洗版
            if not subscribe.best_version:
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在，完成订阅')
                    self.subscribeoper.delete(subscribe.id)
                    # 发送通知
                    self.post_message(Notification(mtype=NotificationType.Subscribe,
                                                   title=f'{mediainfo.title_year} {meta.season} 已完成订阅',
                                                   image=mediainfo.get_message_image()))
                    continue
                # 电视剧订阅
                if meta.type == MediaType.TV:
                    # 使用订阅的总集数和开始集数替换no_exists
                    no_exists = self.__get_subscribe_no_exits(
                        no_exists=no_exists,
                        tmdb_id=mediainfo.tmdb_id,
                        begin_season=meta.begin_season,
                        total_episode=subscribe.total_episode,
                        start_episode=subscribe.start_episode,

                    )
                    # 打印缺失集信息
                    if no_exists and no_exists.get(subscribe.tmdbid):
                        no_exists_info = no_exists.get(subscribe.tmdbid).get(subscribe.season)
                        if no_exists_info:
                            logger.info(f'订阅 {mediainfo.title_year} {meta.season} 缺失集：{no_exists_info.episodes}')
            else:
                # 洗版
                if meta.type == MediaType.TV:
                    no_exists = {
                        subscribe.season: NotExistMediaInfo(
                            season=subscribe.season,
                            episodes=[],
                            total_episode=subscribe.total_episode,
                            start_episode=subscribe.start_episode or 1)
                    }
                else:
                    no_exists = {}
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
                    # 过滤规则
                    if subscribe.best_version:
                        filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules2)
                    else:
                        filter_rule = self.systemconfig.get(SystemConfigKey.FilterRules)
                    result: List[TorrentInfo] = self.filter_torrents(
                        rule_string=filter_rule,
                        torrent_list=[torrent_info])
                    if result is not None and not result:
                        # 不符合过滤规则
                        continue
                    # 不在订阅站点范围的不处理
                    if subscribe.sites:
                        sub_sites = json.loads(subscribe.sites)
                        if sub_sites and torrent_info.site not in sub_sites:
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
                            if no_exists and no_exists.get(subscribe.tmdbid):
                                # 缺失集
                                no_exists_info = no_exists.get(subscribe.tmdbid).get(subscribe.season)
                                if no_exists_info:
                                    # 是否有交集
                                    if no_exists_info.episodes and \
                                            torrent_meta.episode_list and \
                                            not set(no_exists_info.episodes).intersection(
                                                set(torrent_meta.episode_list)
                                            ):
                                        logger.info(
                                            f'{torrent_info.title} 对应剧集 {torrent_meta.episodes} 未包含缺失的剧集')
                                        continue
                            # 过滤掉已经下载的集数
                            if self.__check_subscribe_note(subscribe, torrent_meta.episode_list):
                                logger.info(f'{torrent_info.title} 对应剧集 {torrent_meta.episodes} 已下载过')
                                continue
                        else:
                            # 洗版时，非整季不要
                            if meta.type == MediaType.TV:
                                if torrent_meta.episode_list:
                                    logger.info(f'{subscribe.name} 正在洗版，{torrent_info.title} 不是整季')
                                    continue
                    # 包含
                    if subscribe.include:
                        if not re.search(r"%s" % subscribe.include,
                                         f"{torrent_info.title} {torrent_info.description}", re.I):
                            continue
                    # 排除
                    if subscribe.exclude:
                        if re.search(r"%s" % subscribe.exclude,
                                     f"{torrent_info.title} {torrent_info.description}", re.I):
                            continue
                    # 匹配成功
                    logger.info(f'{mediainfo.title_year} 匹配成功：{torrent_info.title}')
                    _match_context.append(context)
            # 开始下载
            logger.info(f'{mediainfo.title_year} 匹配完成，共匹配到{len(_match_context)}个资源')
            if _match_context:
                # 批量择优下载
                downloads, lefts = self.downloadchain.batch_download(contexts=_match_context, no_exists=no_exists)
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
                        self.__upate_lack_episodes(lefts=lefts, subscribe=subscribe,
                                                   mediainfo=mediainfo, update_date=update_date)
            else:
                if meta.type == MediaType.TV:
                    # 未搜索到资源，但本地缺失可能有变化，更新订阅剩余集数
                    self.__upate_lack_episodes(lefts=no_exists, subscribe=subscribe, mediainfo=mediainfo)

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
            if mediainfo.tmdb_id != subscribe.tmdbid:
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

    def __upate_lack_episodes(self, lefts: Dict[int, Dict[int, NotExistMediaInfo]],
                              subscribe: Subscribe,
                              mediainfo: MediaInfo,
                              update_date: bool = False):
        """
        更新订阅剩余集数
        """
        left_seasons = lefts.get(mediainfo.tmdb_id) or {}
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
                tmdb_link = f"https://www.themoviedb.org/movie/{subscribe.tmdbid}"
                messages.append(f"{subscribe.id}. [{subscribe.name}（{subscribe.year}）]({tmdb_link})")
            else:
                tmdb_link = f"https://www.themoviedb.org/tv/{subscribe.tmdbid}"
                messages.append(f"{subscribe.id}. [{subscribe.name}（{subscribe.year}）]({tmdb_link}) "
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
                                 tmdb_id: int,
                                 begin_season: int,
                                 total_episode: int,
                                 start_episode: int):
        """
        根据订阅开始集数和总结数，结合TMDB信息计算当前订阅的缺失集数
        :param no_exists: 缺失季集列表
        :param tmdb_id: TMDB ID
        :param begin_season: 开始季
        :param total_episode: 总集数
        :param start_episode: 开始集数
        """
        # 使用订阅的总集数和开始集数替换no_exists
        if no_exists \
                and no_exists.get(tmdb_id) \
                and (total_episode or start_episode):
            no_exist_season = no_exists.get(tmdb_id).get(begin_season)
            if no_exist_season:
                # 原季集列表
                episode_list = no_exist_season.episodes
                # 整季缺失且没有开始集数的的不处理
                if not episode_list and not start_episode:
                    return no_exists
                # 原总集数
                total = no_exist_season.total_episode
                if total_episode and start_episode:
                    # 有开始集和总集数
                    episodes = list(range(start_episode, total_episode + 1))
                elif not start_episode:
                    # 有总集数没有开始集
                    episodes = list(range(min(episode_list or [1]), total_episode + 1))
                    start_episode = min(episode_list or [1])
                elif not total_episode:
                    # 有开始集没有总集数
                    episodes = list(range(start_episode, max(episode_list or [total]) + 1))
                    total_episode = max(episode_list or [total])
                else:
                    return no_exists
                # 与原有集取交集
                if episode_list:
                    episodes = list(set(episodes).intersection(set(episode_list)))
                # 处理集合
                no_exists[tmdb_id][begin_season] = NotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start_episode
                )
        return no_exists
