import copy
import json
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Tuple

from app import schemas
from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.helper.interaction import (
    SlashInteractionManager,
    build_navigation_buttons,
    format_markdown_table,
    page_items,
    supports_interaction_buttons,
    supports_markdown,
    update_or_post_message,
)
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings, global_vars
from app.core.context import TorrentInfo, Context, MediaInfo
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.meta.words import WordsMatcher
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.subscribe import Subscribe
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.server import MoviePilotServerHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import (MediaRecognizeConvertEventData, SubscribeEpisodesRefreshEventData,
                         SubscribeCompletionCheckEventData)
from app.schemas.types import MediaType, SystemConfigKey, MessageChannel, NotificationType, EventType, ChainEventType, \
    ContentType


subscribe_interaction_manager = SlashInteractionManager()


class SubscribeChain(ChainBase):
    """
    订阅管理处理链
    """

    _rlock = threading.RLock()
    # 避免莫名原因导致长时间持有锁
    _LOCK_TIMOUT = 3600 * 2
    _button_page_size = 6
    _text_page_size = 10

    @staticmethod
    def __normalize_episode_priority(episode_priority: Optional[dict]) -> Dict[str, int]:
        """
        归一化按集洗版优先级状态。
        """
        if not isinstance(episode_priority, dict):
            return {}

        normalized = {}
        for episode, priority in episode_priority.items():
            if episode is None or priority is None:
                continue
            try:
                normalized[str(int(episode))] = int(priority)
            except (TypeError, ValueError):
                continue
        return normalized

    @classmethod
    def __get_episode_priority(cls, subscribe: Subscribe) -> Dict[str, int]:
        """
        获取订阅按集洗版优先级状态。
        """
        episode_priority = cls.__normalize_episode_priority(getattr(subscribe, "episode_priority", None))
        if episode_priority:
            return episode_priority

        if subscribe.best_version and subscribe.type == MediaType.TV.value and subscribe.current_priority is not None:
            target_episodes = cls.__get_best_version_target_episodes(subscribe)
            return {
                str(episode): int(subscribe.current_priority)
                for episode in target_episodes
            }
        return {}

    @classmethod
    def get_episode_priority(cls, subscribe: Subscribe) -> Dict[str, int]:
        """
        对外暴露按集洗版优先级状态。
        """
        return cls.__get_episode_priority(subscribe)

    @classmethod
    def __get_best_version_target_episodes(cls, subscribe: Subscribe) -> List[int]:
        """
        获取洗版订阅目标剧集范围。
        """
        if subscribe.type != MediaType.TV.value:
            return []

        start_episode = subscribe.start_episode or 1
        total_episode = subscribe.total_episode or 0
        if total_episode < start_episode:
            return []
        return list(range(start_episode, total_episode + 1))

    @classmethod
    def __get_pending_best_version_episodes_with_priority(
            cls,
            subscribe: Subscribe,
            episode_priority: Optional[dict] = None,
    ) -> List[int]:
        """
        使用指定按集优先级状态获取当前仍需继续洗版的剧集。
        """
        target_episodes = cls.__get_best_version_target_episodes(subscribe)
        if not target_episodes:
            return []

        if episode_priority is None:
            normalized = cls.__get_episode_priority(subscribe)
        else:
            normalized = cls.__normalize_episode_priority(episode_priority)
        return [episode for episode in target_episodes if normalized.get(str(episode)) != 100]

    @classmethod
    def _get_pending_best_version_episodes(cls, subscribe: Subscribe) -> List[int]:
        """
        获取当前仍需继续洗版的剧集。
        """
        return cls.__get_pending_best_version_episodes_with_priority(subscribe)

    @classmethod
    def compute_completed_episode(cls, subscribe: Subscribe) -> Optional[int]:
        """
        计算订阅"已完成"集数派生值，仅用于响应填充，不入库。

        语义：
        - 普通订阅 (best_version=0)：``max(total_episode - lack_episode, 0)``，即媒体库已入库集数。
        - 洗版订阅 (best_version=1，含分集与全集洗版)：
          ``(start_episode - 1) + (episode_priority 中 priority==100 且 ep ∈ [start, total] 的命中数)``。
          start_episode 之前的集不在订阅范围内，视为"逻辑上已完成"，与主文案分母 total_episode 对齐。

        - 入参：完整 Subscribe ORM/Schema 对象，需至少包含 best_version、type、start_episode、
          total_episode、lack_episode、episode_priority 字段。
        - 返回：完成集数；电影或缺少 total_episode 时返回 None。
        """
        total_episode = subscribe.total_episode or 0
        if subscribe.type != MediaType.TV.value or not total_episode:
            return None

        start_episode = subscribe.start_episode or 1

        if not subscribe.best_version:
            lack = subscribe.lack_episode or 0
            return max(total_episode - lack, 0)

        # 洗版口径：start 之前的集视为已完成 + 范围内 priority==100 命中。
        # ``start_episode > total_episode`` 是异常配置，需把"起始集前"偏移截断到 total，
        # 避免 completed 越过分母 total_episode。
        episode_priority = subscribe.episode_priority or {}
        priority_completed = sum(
            1
            for ep_key, priority in episode_priority.items()
            if str(ep_key).isdigit()
            and start_episode <= int(ep_key) <= total_episode
            and priority == 100
        )
        return min(max(start_episode - 1, 0), total_episode) + priority_completed

    @classmethod
    def get_best_version_current_priority(
            cls,
            subscribe: Subscribe,
            episode_priority: Optional[dict] = None,
    ) -> int:
        """
        获取洗版订阅当前优先级状态。
        """
        if not subscribe.best_version or subscribe.type != MediaType.TV.value:
            return subscribe.current_priority or 0

        pending_episodes = cls.__get_pending_best_version_episodes_with_priority(subscribe, episode_priority)
        if not pending_episodes:
            return 100

        if episode_priority is None:
            normalized = cls.__get_episode_priority(subscribe)
        else:
            normalized = cls.__normalize_episode_priority(episode_priority)
        return max(
            (normalized.get(str(episode), 0) for episode in pending_episodes),
            default=0,
        )

    @classmethod
    def __is_best_version_complete(cls, subscribe: Subscribe) -> bool:
        """
        判断洗版订阅是否已完成。
        """
        if not subscribe.best_version:
            return False
        if subscribe.type != MediaType.TV.value:
            return subscribe.current_priority == 100

        target_episodes = cls.__get_best_version_target_episodes(subscribe)
        if not target_episodes:
            return subscribe.current_priority == 100

        episode_priority = cls.__get_episode_priority(subscribe)
        return all(episode_priority.get(str(episode)) == 100 for episode in target_episodes)

    @classmethod
    def is_best_version_complete(cls, subscribe: Subscribe) -> bool:
        """
        对外暴露洗版完成判断。
        """
        return cls.__is_best_version_complete(subscribe)

    @classmethod
    def __is_best_version_complete_with_priority(
            cls,
            subscribe: Subscribe,
            episode_priority: Optional[dict] = None,
    ) -> bool:
        """
        使用指定按集优先级状态判断洗版是否已完成。
        """
        if not subscribe.best_version:
            return False
        if subscribe.type != MediaType.TV.value:
            return subscribe.current_priority == 100

        target_episodes = cls.__get_best_version_target_episodes(subscribe)
        if not target_episodes:
            return subscribe.current_priority == 100

        return not cls.__get_pending_best_version_episodes_with_priority(subscribe, episode_priority)

    @staticmethod
    def __get_downloaded_episodes(downloads: Optional[List[Context]]) -> List[int]:
        """
        获取本次下载实际涉及的剧集。
        """
        if not downloads:
            return []

        downloaded_episodes = set()
        for context in downloads:
            selected_episodes = getattr(context, "selected_episodes", None)
            if selected_episodes is None:
                selected_episodes = context.meta_info.episode_list if context.meta_info else []
            for episode in selected_episodes or []:
                try:
                    downloaded_episodes.add(int(episode))
                except (TypeError, ValueError):
                    continue
        return sorted(downloaded_episodes)

    @classmethod
    def __get_best_version_completed_episodes(cls, subscribe: Subscribe) -> List[int]:
        """
        获取已完成洗版的剧集。
        """
        episode_priority = cls.__get_episode_priority(subscribe)
        return sorted(
            int(episode) for episode, priority in episode_priority.items()
            if str(episode).isdigit() and priority == 100
        )

    @classmethod
    def __get_best_version_interested_episodes(
            cls,
            subscribe: Subscribe,
            context: Context,
            priority: int,
    ) -> List[int]:
        """
        获取当前资源中仍值得继续洗版的剧集。
        """
        if subscribe.type != MediaType.TV.value:
            return []

        target_episodes = set(cls.__get_best_version_target_episodes(subscribe))
        if not target_episodes:
            return []

        selected_episodes = getattr(context, "selected_episodes", None)
        if selected_episodes is None:
            selected_episodes = context.meta_info.episode_list if context.meta_info else []
        if not selected_episodes:
            episode_priority = cls.__get_episode_priority(subscribe)
            return sorted([
                episode for episode in target_episodes
                if episode_priority.get(str(episode)) is None or priority > episode_priority.get(str(episode))
            ])

        episode_priority = cls.__get_episode_priority(subscribe)
        interested = []
        for episode in selected_episodes:
            try:
                episode_num = int(episode)
            except (TypeError, ValueError):
                continue
            if episode_num not in target_episodes:
                continue
            current_priority = episode_priority.get(str(episode_num))
            if current_priority is None or priority > current_priority:
                interested.append(episode_num)
        return sorted(set(interested))

    @classmethod
    def __is_full_best_version_enabled(cls, subscribe: Subscribe) -> bool:
        """
        判断当前订阅是否启用了电视剧全集洗版。
        """
        return (
            bool(getattr(subscribe, "best_version_full", 0))
            and bool(subscribe.best_version)
            and subscribe.type == MediaType.TV.value
        )

    @classmethod
    def __is_full_season_resource(cls, meta: MetaBase, subscribe: Subscribe) -> bool:
        """
        判断候选资源是否覆盖订阅目标全集范围。
        """
        season_list = meta.season_list or [1]
        if len(season_list) != 1:
            return False
        if subscribe.season is not None and season_list[0] != subscribe.season:
            return False

        episodes = meta.episode_list
        if not episodes:
            # 资源未标出单集时按整季包处理，后续下载前仍会解析种子文件确认完整性。
            return True

        target_episodes = set(cls.__get_best_version_target_episodes(subscribe))
        if not target_episodes:
            return False
        return target_episodes.issubset(set(episodes))

    @classmethod
    def __is_full_season_best_version_resource(cls, meta: MetaBase, subscribe: Subscribe) -> bool:
        """
        判断候选资源是否符合全集洗版资源约束。
        """
        if not cls.__is_full_best_version_enabled(subscribe):
            return True

        return cls.__is_full_season_resource(meta=meta, subscribe=subscribe)

    @classmethod
    def __is_full_season_priority_higher_than_all_targets(cls, subscribe: Subscribe, priority: int) -> bool:
        """
        判断整季资源优先级是否高于订阅目标范围内所有分集。
        """
        if subscribe.type != MediaType.TV.value:
            return False

        target_episodes = cls.__get_best_version_target_episodes(subscribe)
        if not target_episodes:
            return False

        try:
            resource_priority = int(priority or 0)
        except (TypeError, ValueError):
            resource_priority = 0

        episode_priority = cls.__get_episode_priority(subscribe)
        for episode in target_episodes:
            current_priority = episode_priority.get(str(episode), 0)
            if resource_priority <= current_priority:
                return False
        return True

    @classmethod
    def __build_full_pack_first_no_exists(
            cls,
            subscribe: Subscribe,
            mediakey: Union[int, str],
    ) -> Optional[Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]]]:
        """
        构造分集洗版优先全集时使用的整季缺失范围。
        """
        if (
            not subscribe.best_version
            or cls.__is_full_best_version_enabled(subscribe)
            or subscribe.type != MediaType.TV.value
        ):
            return None

        target_episodes = cls.__get_best_version_target_episodes(subscribe)
        if not target_episodes:
            return None

        return {
            mediakey: {
                subscribe.season: schemas.NotExistMediaInfo(
                    season=subscribe.season,
                    episodes=[],
                    total_episode=subscribe.total_episode,
                    start_episode=subscribe.start_episode or 1,
                )
            }
        }

    def __download_best_version_with_full_pack_first(
            self,
            contexts: List[Context],
            no_exists: Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]],
            subscribe: Subscribe,
            mediakey: Union[int, str],
            username: Optional[str] = None,
            save_path: Optional[str] = None,
            downloader: Optional[str] = None,
            source: Optional[str] = None,
    ) -> Tuple[List[Context], Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]]]:
        """
        TV 分集洗版先尝试覆盖目标范围的全集资源，失败后回退到按集下载。
        """
        full_pack_no_exists = self.__build_full_pack_first_no_exists(subscribe=subscribe, mediakey=mediakey)
        full_season_contexts = [
            context for context in contexts
            if context.media_info.type == MediaType.TV
            and self.__is_full_season_resource(meta=context.meta_info, subscribe=subscribe)
        ] if full_pack_no_exists else []
        full_pack_contexts = [
            context for context in full_season_contexts
            if self.__is_full_season_priority_higher_than_all_targets(
                subscribe=subscribe,
                priority=context.torrent_info.pri_order,
            )
        ]

        if full_season_contexts and not full_pack_contexts:
            logger.info(f"{subscribe.name} 全集候选优先级未高于所有目标集，回退到分集洗版")

        if full_pack_contexts:
            logger.info(f"{subscribe.name} 分集洗版优先尝试全集资源，共匹配到 {len(full_pack_contexts)} 个候选")
            downloads, lefts = DownloadChain().batch_download(
                contexts=full_pack_contexts,
                no_exists=full_pack_no_exists,
                username=username,
                save_path=save_path,
                downloader=downloader,
                source=source,
            )
            if downloads:
                return downloads, lefts
            logger.info(f"{subscribe.name} 未下载到全集资源，回退到分集洗版")

        return DownloadChain().batch_download(
            contexts=contexts,
            no_exists=no_exists,
            username=username,
            save_path=save_path,
            downloader=downloader,
            source=source,
        )

    @staticmethod
    def __get_event_media(_mediaid: str, _meta: MetaBase) -> Optional[MediaInfo]:
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
                mediachain = MediaChain()
                new_id = event_data.media_dict.get("id")
                if event_data.convert_type == "themoviedb":
                    return mediachain.recognize_media(meta=_meta, tmdbid=new_id)
                elif event_data.convert_type == "douban":
                    return mediachain.recognize_media(meta=_meta, doubanid=new_id)
        return None

    @staticmethod
    async def __async_get_event_meida(_mediaid: str, _meta: MetaBase) -> Optional[MediaInfo]:
        """
        广播事件解析媒体信息
        """
        event_data = MediaRecognizeConvertEventData(
            mediaid=_mediaid,
            convert_type=settings.RECOGNIZE_SOURCE
        )
        event = await eventmanager.async_send_event(ChainEventType.MediaRecognizeConvert, event_data)
        # 使用事件返回的上下文数据
        if event and event.event_data:
            event_data: MediaRecognizeConvertEventData = event.event_data
            if event_data.media_dict:
                mediachain = MediaChain()
                new_id = event_data.media_dict.get("id")
                if event_data.convert_type == "themoviedb":
                    return await mediachain.async_recognize_media(meta=_meta, tmdbid=new_id)
                elif event_data.convert_type == "douban":
                    return await mediachain.async_recognize_media(meta=_meta, doubanid=new_id)
        return None

    def __get_default_kwargs(self, mtype: MediaType, **kwargs) -> dict:
        """
        获取订阅默认配置
        :param mtype: 媒体类型
        :param key: 配置键
        :return: 配置值
        """
        return {
            'quality': self.__get_default_subscribe_config(mtype, "quality") if not kwargs.get(
                "quality") else kwargs.get("quality"),
            'resolution': self.__get_default_subscribe_config(mtype, "resolution") if not kwargs.get(
                "resolution") else kwargs.get("resolution"),
            'effect': self.__get_default_subscribe_config(mtype, "effect") if not kwargs.get(
                "effect") else kwargs.get("effect"),
            'include': self.__get_default_subscribe_config(mtype, "include") if not kwargs.get(
                "include") else kwargs.get("include"),
            'exclude': self.__get_default_subscribe_config(mtype, "exclude") if not kwargs.get(
                "exclude") else kwargs.get("exclude"),
            'best_version': self.__get_default_subscribe_config(mtype, "best_version") if not kwargs.get(
                "best_version") else kwargs.get("best_version"),
            'best_version_full': self.__get_default_subscribe_config(mtype, "best_version_full")
            if kwargs.get("best_version_full") is None else kwargs.get("best_version_full"),
            'search_imdbid': self.__get_default_subscribe_config(mtype, "search_imdbid") if not kwargs.get(
                "search_imdbid") else kwargs.get("search_imdbid"),
            'sites': self.__get_default_subscribe_config(mtype, "sites") or None if not kwargs.get(
                "sites") else kwargs.get("sites"),
            'downloader': self.__get_default_subscribe_config(mtype, "downloader") if not kwargs.get(
                "downloader") else kwargs.get("downloader"),
            'save_path': self.__get_default_subscribe_config(mtype, "save_path") if not kwargs.get(
                "save_path") else kwargs.get("save_path"),
            'filter_groups': self.__get_default_subscribe_config(mtype, "filter_groups") if not kwargs.get(
                "filter_groups") else kwargs.get("filter_groups")
        }

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

        logger.info(f'开始添加订阅，标题：{title} ...')

        mediainfo = None
        metainfo = MetaInfo(title)
        if year:
            metainfo.year = year
        if mtype:
            metainfo.type = mtype
        if season is not None:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # TMDB识别模式
            if not tmdbid:
                if doubanid:
                    # 将豆瓣信息转换为TMDB信息
                    tmdbinfo = MediaChain().get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
                    if tmdbinfo:
                        mediainfo = MediaInfo(tmdb_info=tmdbinfo)
                elif mediaid:
                    # 未知前缀，广播事件解析媒体信息
                    mediainfo = self.__get_event_media(mediaid, metainfo)
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
                mediainfo = self.__get_event_media(mediaid, metainfo)
            if mediainfo:
                # 豆瓣标题处理
                meta = MetaInfo(mediainfo.title)
                mediainfo.title = meta.name
                if season is None:
                    season = meta.begin_season

        # 使用名称识别兜底
        if not mediainfo:
            mediainfo = MediaChain().recognize_by_meta(
                metainfo,
                episode_group=episode_group,
                obtain_images=False,
            )

        # 识别失败
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}，doubanid：{doubanid}')
            return None, "未识别到媒体信息"

        # 总集数
        if mediainfo.type == MediaType.TV:
            if season is None:
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
                # 允许外部覆盖按 TMDB 算出的总集数（如待定集数）
                total_episode = self.__apply_episodes_refresh(
                    total_episode, season=season, mediainfo=mediainfo,
                    tmdbid=mediainfo.tmdb_id, doubanid=mediainfo.douban_id, scene="create")
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
        kwargs.update(self.__get_default_kwargs(mediainfo.type, **kwargs))

        # 操作数据库
        sid, err_msg = SubscribeOper().add(mediainfo=mediainfo, season=season, username=username, **kwargs)
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
            if mediainfo.type == MediaType.TV:
                link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
            else:
                link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
            # 订阅成功按规则发送消息
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    mtype=NotificationType.Subscribe,
                    ctype=ContentType.SubscribeAdded,
                    image=mediainfo.get_message_image(),
                    link=link,
                    userid=userid,
                    username=username
                ),
                meta=metainfo,
                mediainfo=mediainfo,
                username=username
            )
        # 发送事件
        eventmanager.send_event(EventType.SubscribeAdded, {
            "subscribe_id": sid,
            "username": username,
            "mediainfo": mediainfo.to_dict(),
        })
        # 统计订阅
        MoviePilotServerHelper.sub_reg_async({
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
        return sid, err_msg

    async def async_add(self, title: str, year: str,
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
        异步识别媒体信息并添加订阅
        """

        logger.info(f'开始添加订阅，标题：{title} ...')

        mediainfo = None
        metainfo = MetaInfo(title)
        if year:
            metainfo.year = year
        if mtype:
            metainfo.type = mtype
        if season is not None:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        # 识别媒体信息
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # TMDB识别模式
            if not tmdbid:
                if doubanid:
                    # 将豆瓣信息转换为TMDB信息
                    tmdbinfo = await MediaChain().async_get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
                    if tmdbinfo:
                        mediainfo = MediaInfo(tmdb_info=tmdbinfo)
                elif mediaid:
                    # 未知前缀，广播事件解析媒体信息
                    mediainfo = await self.__async_get_event_meida(mediaid, metainfo)
            else:
                # 使用TMDBID识别
                mediainfo = await self.async_recognize_media(meta=metainfo, mtype=mtype, tmdbid=tmdbid,
                                                             episode_group=episode_group, cache=False)
        else:
            if doubanid:
                # 豆瓣识别模式，不使用缓存
                mediainfo = await self.async_recognize_media(meta=metainfo, mtype=mtype, doubanid=doubanid, cache=False)
            elif mediaid:
                # 未知前缀，广播事件解析媒体信息
                mediainfo = await self.__async_get_event_meida(mediaid, metainfo)
            if mediainfo:
                # 豆瓣标题处理
                meta = MetaInfo(mediainfo.title)
                mediainfo.title = meta.name
                if season is None:
                    season = meta.begin_season

        # 使用名称识别兜底
        if not mediainfo:
            mediainfo = await MediaChain().async_recognize_by_meta(
                metainfo,
                episode_group=episode_group,
                obtain_images=False,
            )

        # 识别失败
        if not mediainfo:
            logger.warn(f'未识别到媒体信息，标题：{title}，tmdbid：{tmdbid}，doubanid：{doubanid}')
            return None, "未识别到媒体信息"

        # 总集数
        if mediainfo.type == MediaType.TV:
            if season is None:
                season = 1
            # 总集数
            if not kwargs.get('total_episode'):
                if not mediainfo.seasons or episode_group:
                    # 补充媒体信息
                    mediainfo = await self.async_recognize_media(mtype=mediainfo.type,
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
                # 允许外部覆盖按 TMDB 算出的总集数（如待定集数）
                total_episode = await self.__async_apply_episodes_refresh(
                    total_episode, season=season, mediainfo=mediainfo,
                    tmdbid=mediainfo.tmdb_id, doubanid=mediainfo.douban_id, scene="create")
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
        await self.async_obtain_images(mediainfo=mediainfo)
        # 合并信息
        if doubanid:
            mediainfo.douban_id = doubanid
        if bangumiid:
            mediainfo.bangumi_id = bangumiid

        # 列新默认参数
        kwargs.update(self.__get_default_kwargs(mediainfo.type, **kwargs))

        # 操作数据库
        sid, err_msg = await SubscribeOper().async_add(mediainfo=mediainfo, season=season, username=username, **kwargs)
        if not sid:
            logger.error(f'{mediainfo.title_year} {err_msg}')
            if not exist_ok and message:
                # 失败发回原用户
                await self.async_post_message(schemas.Notification(channel=channel,
                                                                   source=source,
                                                                   mtype=NotificationType.Subscribe,
                                                                   title=f"{mediainfo.title_year} {metainfo.season} "
                                                                         f"添加订阅失败！",
                                                                   text=f"{err_msg}",
                                                                   image=mediainfo.get_message_image(),
                                                                   userid=userid))
            return None, err_msg
        elif message:
            if mediainfo.type == MediaType.TV:
                link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
            else:
                link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
            # 订阅成功按规则发送消息
            await self.async_post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    mtype=NotificationType.Subscribe,
                    ctype=ContentType.SubscribeAdded,
                    image=mediainfo.get_message_image(),
                    link=link,
                    userid=userid,
                    username=username
                ),
                meta=metainfo,
                mediainfo=mediainfo,
                username=username
            )
        # 发送事件
        await eventmanager.async_send_event(EventType.SubscribeAdded, {
            "subscribe_id": sid,
            "username": username,
            "mediainfo": mediainfo.to_dict(),
        })
        # 统计订阅
        await MoviePilotServerHelper.async_sub_reg({
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
        return sid, err_msg

    @staticmethod
    def exists(mediainfo: MediaInfo, meta: MetaBase = None):
        """
        判断订阅是否已存在
        """
        if SubscribeOper().exists(tmdbid=mediainfo.tmdb_id,
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
        lock_acquired = False
        try:
            if lock_acquired := self._rlock.acquire(
                    blocking=True, timeout=self._LOCK_TIMOUT
            ):
                logger.debug(f"search lock acquired at {datetime.now()}")
            else:
                logger.warn("search上锁超时")

            subscribeoper = SubscribeOper()
            if sid:
                subscribe = subscribeoper.get(sid)
                subscribes = [subscribe] if subscribe else []
            else:
                subscribes = subscribeoper.list(self.get_states_for_search(state))

            try:
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
                        meta.begin_season = subscribe.season if subscribe.season is not None else None
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
                                          or SystemConfigOper().get(SystemConfigKey.BestVersionFilterRuleGroups) or []
                        else:
                            rule_groups = subscribe.filter_groups \
                                          or SystemConfigOper().get(SystemConfigKey.SubscribeFilterRuleGroups) or []

                        # 搜索，同时电视剧会过滤掉不需要的剧集
                        contexts = SearchChain().process(mediainfo=mediainfo,
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
                        try:
                            for context in contexts:
                                if global_vars.is_system_stopped:
                                    break
                                torrent_meta = context.meta_info
                                torrent_info = context.torrent_info
                                torrent_mediainfo = context.media_info

                                # 洗版
                                if subscribe.best_version:
                                    if (
                                        torrent_mediainfo.type == MediaType.TV
                                        and not self.__is_full_season_best_version_resource(
                                            meta=torrent_meta, subscribe=subscribe
                                        )
                                    ):
                                        logger.info(
                                            f"{subscribe.name} 正在全集洗版，{torrent_info.title} 不是全集资源"
                                        )
                                        continue
                                    # 洗版时，不符合订阅集数的不要
                                    if (
                                        torrent_mediainfo.type == MediaType.TV
                                        and not self._is_episode_range_covered(
                                            meta=torrent_meta, subscribe=subscribe
                                        )
                                    ):
                                        logger.info(
                                            f"{subscribe.name} 正在洗版，{torrent_info.title} 不符合订阅集数范围"
                                        )
                                        continue
                                    # 洗版时，只保留至少能提升一集优先级的资源
                                    if torrent_mediainfo.type == MediaType.TV:
                                        interested_episodes = self.__get_best_version_interested_episodes(
                                            subscribe=subscribe,
                                            context=context,
                                            priority=torrent_info.pri_order,
                                        )
                                        if not interested_episodes:
                                            logger.info(
                                                f'{subscribe.name} 正在洗版，{torrent_info.title} 不包含可提升优先级的剧集')
                                            continue
                                        # 将"本候选实际能升级到的集"作为允许下载集合下传到下载层，
                                        # 防止标题元数据与实际种子文件错位导致同优先级集被重复下载。
                                        context.allowed_episodes = set(interested_episodes)
                                    if (
                                        torrent_mediainfo.type != MediaType.TV
                                        and subscribe.current_priority
                                        and torrent_info.pri_order <= subscribe.current_priority
                                    ):
                                        logger.info(
                                            f'{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级')
                                        continue
                                # 更新订阅自定义属性
                                if subscribe.media_category:
                                    torrent_mediainfo.category = subscribe.media_category
                                if subscribe.episode_group:
                                    torrent_mediainfo.episode_group = subscribe.episode_group
                                matched_contexts.append(context)
                        finally:
                            contexts.clear()
                            del contexts

                        if not matched_contexts:
                            logger.warn(f'订阅 {subscribe.name} 没有符合过滤条件的资源')
                            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta,
                                                         mediainfo=mediainfo, lefts=no_exists)
                            continue

                        # 自动下载
                        downloads, lefts = self.__download_best_version_with_full_pack_first(
                            contexts=matched_contexts,
                            no_exists=no_exists,
                            subscribe=subscribe,
                            mediakey=mediakey,
                            username=subscribe.username,
                            save_path=subscribe.save_path,
                            downloader=subscribe.downloader,
                            source=self.get_subscribe_source_keyword(subscribe)
                        )

                        # 同步外部修改，更新订阅信息
                        subscribe = subscribeoper.get(subscribe.id)

                        # 判断是否应完成订阅
                        if subscribe:
                            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo,
                                                         downloads=downloads, lefts=lefts)
                    finally:
                        # 如果状态为N则更新为R
                        if subscribe and subscribe.state == 'N':
                            subscribeoper.update(subscribe.id, {'state': 'R'})

                # 手动触发时发送系统消息
                if manual:
                    if subscribes:
                        if sid:
                            self.messagehelper.put(f'{subscribes[0].name} 搜索完成！', title="订阅搜索", role="system")
                        else:
                            self.messagehelper.put('所有订阅搜索完成！', title="订阅搜索", role="system")
                    else:
                        self.messagehelper.put('没有找到订阅！', title="订阅搜索", role="system")

            finally:
                subscribes.clear()
                del subscribes
        finally:
            if lock_acquired:
                self._rlock.release()
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
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if subscribe.type == MediaType.TV.value:
            episode_priority = self.__get_episode_priority(subscribe)
            updated = False
            for download in downloads:
                download_priority = download.torrent_info.pri_order
                downloaded_episodes = self.__get_downloaded_episodes([download])
                if not downloaded_episodes and self.__is_full_season_resource(download.meta_info, subscribe):
                    # 整包下载时资源标题常不携带集数，视为覆盖当前订阅的全部目标集。
                    downloaded_episodes = self.__get_best_version_target_episodes(subscribe)
                if not downloaded_episodes:
                    continue
                for episode in downloaded_episodes:
                    episode_key = str(episode)
                    old_priority = episode_priority.get(episode_key)
                    if old_priority is None or download_priority > old_priority:
                        episode_priority[episode_key] = download_priority
                        updated = True

            if not updated and not episode_priority:
                return

            current_priority = self.get_best_version_current_priority(subscribe, episode_priority)
            # lack_episode 由 finish_subscribe_or_not -> __update_lack_episodes 按媒体库实况维护，本处不写
            update_data: Dict[str, Any] = {
                "episode_priority": episode_priority,
                "last_update": now,
                "current_priority": current_priority,
            }

            SubscribeOper().update(subscribe.id, update_data)
            subscribe.episode_priority = episode_priority
            subscribe.current_priority = current_priority
            subscribe.last_update = now

            completed_episodes = self.__get_best_version_completed_episodes(subscribe)
            if self.__is_best_version_complete(subscribe):
                logger.info(f'{mediainfo.title_year} 洗版完成，已完成剧集：{completed_episodes}')
                self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
            else:
                logger.info(
                    f'{mediainfo.title_year} 正在洗版，更新剧集优先级为 {priority}，已完成剧集：{completed_episodes}'
                )
            return

        # 订阅存在待定策略，不管是否已完成，均需更新订阅信息
        SubscribeOper().update(subscribe.id, {
            "current_priority": priority,
            "last_update": now
        })
        subscribe.current_priority = priority
        subscribe.last_update = now
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
        # 不论是否洗版，只要本轮有下载产生就要把集数追加进 subscribe.note，
        # 保证"已下载过哪些集"这条事实在所有订阅模式下都有可靠落点；洗版分支
        # 之前只写 episode_priority，导致用户切回普通订阅时丢失下载历史，并让
        # __get_downloaded 在洗版下无法从 note 拿到 priority 未达 100 但实际下过的集。
        if downloads:
            self.__update_subscribe_note(subscribe=subscribe, downloads=downloads)
        # 是否完成订阅
        if not subscribe.best_version:
            # 普通订阅：先按 lefts 写 lack，再判断完成
            self.__update_lack_episodes(lefts=lefts, subscribe=subscribe, mediainfo=mediainfo,
                                        update_date=bool(downloads))
            if ((no_lefts and meta.type == MediaType.TV)
                    or (downloads and meta.type == MediaType.MOVIE)
                    or force):
                self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
            else:
                logger.info(f'{mediainfo.title_year} 未下载完整，继续订阅 ...')
            return

        # 洗版订阅：本轮若有下载先更新 episode_priority / current_priority，让 __update_lack_episodes
        # 读取到包含本轮新下载的集；否则 lack 会慢一个搜索周期才反映新下载。
        if downloads:
            self.update_subscribe_priority(subscribe=subscribe, meta=meta,
                                           mediainfo=mediainfo, downloads=downloads)
        self.__update_lack_episodes(lefts=lefts, subscribe=subscribe, mediainfo=mediainfo,
                                    update_date=bool(downloads))
        if self.__is_best_version_complete(subscribe):
            # 洗版完成
            self.__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
        elif not downloads:
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
            TorrentsChain().refresh(sites=sites)
        )

    @staticmethod
    def get_sub_sites(subscribe: Subscribe) -> List[int]:
        """
        获取订阅中涉及的站点清单
        :param subscribe: 订阅信息对象
        :return: 涉及的站点清单
        """
        # 从系统配置获取默认订阅站点
        default_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []
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
        ret_sites = []
        subscribes = SubscribeOper().list()
        if not subscribes:
            # 没有订阅
            return None
        # 刷新订阅选中的Rss站点
        for subscribe in subscribes:
            # 刷新选中的站点
            if subscribe.state in self.get_states_for_search('R'):
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

        lock_acquired = False
        try:
            if lock_acquired := self._rlock.acquire(
                    blocking=True, timeout=self._LOCK_TIMOUT
            ):
                logger.debug(f"match lock acquired at {datetime.now()}")
            else:
                logger.warn("match上锁超时")

            # 预识别所有未识别的种子
            processed_torrents: Dict[str, List[Context]] = {}
            for domain, contexts in torrents.items():
                if global_vars.is_system_stopped:
                    break
                processed_torrents[domain] = []
                for context in contexts:
                    if global_vars.is_system_stopped:
                        break
                    # 如果种子未识别且失败次数未超过3次，尝试识别
                    if (not context.media_info or (not context.media_info.tmdb_id
                                                   and not context.media_info.douban_id)) and context.media_recognize_fail_count < 3:
                        logger.debug(
                            f'尝试重新识别种子：{context.torrent_info.title}，当前失败次数：{context.media_recognize_fail_count}/3')
                        re_mediainfo = MediaChain().recognize_by_meta(
                            context.meta_info,
                            obtain_images=False,
                        )
                        if re_mediainfo:
                            # 清理多余信息
                            re_mediainfo.clear()
                            # 更新种子缓存
                            context.media_info = re_mediainfo
                            context.match_source = self.__get_media_id_match_source(re_mediainfo)
                            context.candidate_recognized = bool(
                                re_mediainfo.tmdb_id or re_mediainfo.douban_id
                            )
                            context.media_info_is_target = False
                            # 重置失败次数
                            context.media_recognize_fail_count = 0
                            logger.debug(f'种子 {context.torrent_info.title} 重新识别成功')
                        else:
                            # 识别失败，增加失败次数
                            context.media_recognize_fail_count += 1
                            logger.debug(
                                f'种子 {context.torrent_info.title} 媒体识别失败，失败次数：{context.media_recognize_fail_count}/3')
                    elif context.media_recognize_fail_count >= 3:
                        logger.debug(f'种子 {context.torrent_info.title} 已达到最大识别失败次数(3次)，跳过识别')
                    # 添加已预处理
                    processed_torrents[domain].append(context)

            # 所有订阅
            subscribes = SubscribeOper().list(self.get_states_for_search('R'))
            try:
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
                        domains = SiteOper().get_domains_by_ids(subscribe.sites)
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

                    # 清理多余信息
                    mediainfo.clear()

                    # 订阅识别词
                    if subscribe.custom_words:
                        custom_words_list = subscribe.custom_words.split("\n")
                    else:
                        custom_words_list = None

                    # 遍历预识别后的种子
                    _match_context = []
                    torrenthelper = TorrentHelper()
                    systemconfig = SystemConfigOper()
                    wordsmatcher = WordsMatcher()
                    for domain, contexts in processed_torrents.items():
                        if global_vars.is_system_stopped:
                            break
                        if domains and domain not in domains:
                            continue
                        logger.debug(f'开始匹配站点：{domain}，共缓存了 {len(contexts)} 个种子...')
                        for context in contexts:
                            if global_vars.is_system_stopped:
                                break
                            # 提取信息
                            _context = copy.copy(context)
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
                                _, apply_words = wordsmatcher.prepare(torrent_meta.org_string,
                                                                      custom_words=custom_words_list)
                                if apply_words:
                                    logger.info(
                                        f'{torrent_info.site_name} - {torrent_info.title} 因订阅存在自定义识别词，重新识别元数据...')
                                    # 重新识别元数据
                                    torrent_meta = MetaInfo(title=torrent_info.title, subtitle=torrent_info.description,
                                                            custom_words=custom_words_list)
                                    # 更新元数据缓存
                                    _context.meta_info = torrent_meta
                                    # 重新识别媒体信息
                                    torrent_mediainfo = MediaChain().recognize_by_meta(
                                        torrent_meta,
                                        episode_group=subscribe.episode_group,
                                        obtain_images=False,
                                    )
                                    if torrent_mediainfo:
                                        # 清理多余信息
                                        torrent_mediainfo.clear()
                                        # 更新种子缓存
                                        _context.media_info = torrent_mediainfo
                                        _context.match_source = self.__get_media_id_match_source(torrent_mediainfo)
                                        _context.candidate_recognized = bool(
                                            torrent_mediainfo.tmdb_id or torrent_mediainfo.douban_id
                                        )
                                        _context.media_info_is_target = False

                            # 如果仍然没有识别到媒体信息，尝试标题匹配
                            if not torrent_mediainfo or (
                                    not torrent_mediainfo.tmdb_id and not torrent_mediainfo.douban_id):
                                logger.debug(
                                    f'{torrent_info.site_name} - {torrent_info.title} 重新识别失败，尝试通过标题匹配...')
                                if TorrentHelper.match_torrent(mediainfo=mediainfo,
                                                               torrent_meta=torrent_meta,
                                                               torrent=torrent_info):
                                    # 匹配成功
                                    logger.info(
                                        f'{mediainfo.title_year} 通过标题匹配到可选资源：{torrent_info.site_name} - {torrent_info.title}')
                                    torrent_mediainfo = mediainfo
                                    # 更新种子缓存
                                    _context.media_info = mediainfo
                                    _context.match_source = "title"
                                    _context.candidate_recognized = False
                                    _context.media_info_is_target = True
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
                                    f'{mediainfo.title_year} 通过媒体ID匹配到可选资源：{torrent_info.site_name} - {torrent_info.title}')
                                match_source = getattr(_context, "match_source", "unknown")
                                if match_source == "title":
                                    # 标题兜底使用的是订阅目标 media_info，不能标记为候选自身识别结果。
                                    _context.candidate_recognized = False
                                    _context.media_info_is_target = True
                                elif match_source == "unknown":
                                    _context.match_source = self.__get_media_id_match_source(torrent_mediainfo)
                                    _context.candidate_recognized = True
                                    _context.media_info_is_target = False
                                else:
                                    _context.candidate_recognized = True
                                    _context.media_info_is_target = False
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
                                    if not self.__is_full_season_best_version_resource(
                                        meta=torrent_meta,
                                        subscribe=subscribe,
                                    ):
                                        logger.debug(
                                            f"{subscribe.name} 正在全集洗版，{torrent_info.title} 不是全集资源"
                                        )
                                        continue
                                    # 洗版时，不符合订阅集数的不要
                                    if (
                                        meta.type == MediaType.TV
                                        and not self._is_episode_range_covered(
                                            meta=torrent_meta,
                                            subscribe=subscribe,
                                        )
                                    ):
                                        logger.debug(
                                            f"{subscribe.name} 正在洗版，{torrent_info.title} 不符合订阅集数范围"
                                        )
                                        continue

                            # 匹配订阅附加参数
                            if not torrenthelper.filter_torrent(torrent_info=torrent_info,
                                                                filter_params=self.get_params(subscribe)):
                                continue

                            # 优先级过滤规则
                            if subscribe.best_version:
                                rule_groups = subscribe.filter_groups \
                                              or systemconfig.get(SystemConfigKey.BestVersionFilterRuleGroups)
                            else:
                                rule_groups = subscribe.filter_groups \
                                              or systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
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
                                if meta.type == MediaType.TV:
                                    interested_episodes = self.__get_best_version_interested_episodes(
                                        subscribe=subscribe,
                                        context=_context,
                                        priority=torrent_info.pri_order,
                                    )
                                    if not interested_episodes:
                                        logger.info(
                                            f'{subscribe.name} 正在洗版，{torrent_info.title} 不包含可提升优先级的剧集')
                                        continue
                                    # 与 search() 路径对称：把"本候选实际能升级到的集"作为允许下载集合下传到下载层，
                                    # 避免 RSS / 订阅刷新场景下标题元数据与种子文件错位导致同优先级集重复下载。
                                    _context.allowed_episodes = set(interested_episodes)
                                if (
                                    meta.type != MediaType.TV
                                    and subscribe.current_priority
                                    and torrent_info.pri_order <= subscribe.current_priority
                                ):
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
                    downloads, lefts = self.__download_best_version_with_full_pack_first(
                        contexts=_match_context,
                        no_exists=no_exists,
                        subscribe=subscribe,
                        mediakey=mediakey,
                        username=subscribe.username,
                        save_path=subscribe.save_path,
                        downloader=subscribe.downloader,
                        source=self.get_subscribe_source_keyword(subscribe)
                    )

                    # 同步外部修改，更新订阅信息
                    subscribe = SubscribeOper().get(subscribe.id)

                    # 判断是否要完成订阅
                    if subscribe:
                        self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo,
                                                     downloads=downloads, lefts=lefts)
            finally:
                processed_torrents.clear()
                del processed_torrents
                subscribes.clear()
                del subscribes
        finally:
            if lock_acquired:
                self._rlock.release()
                logger.debug(f"match Lock released at {datetime.now()}")

    def check(self):
        """
        定时检查订阅，更新订阅信息
        """
        # 查询所有订阅
        subscribeoper = SubscribeOper()
        # 遍历订阅
        for subscribe in subscribeoper.list():
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
            current_priority = None
            if not subscribe.manual_total_episode and len(episodes):
                total_episode = len(episodes)
                # 允许外部覆盖按 TMDB 算出的总集数（如待定集数）
                total_episode = self.__apply_episodes_refresh(
                    total_episode, season=subscribe.season, mediainfo=mediainfo,
                    tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid,
                    subscribe_id=subscribe.id, scene="refresh")
                lack_episode = max((subscribe.lack_episode or 0) + (total_episode - (subscribe.total_episode or 0)), 0)
                if subscribe.best_version and subscribe.type == MediaType.TV.value:
                    # 为新增集补齐 episode_priority 初始项（priority=0）
                    old_total_episode = subscribe.total_episode or 0
                    episode_priority = self.__get_episode_priority(subscribe)
                    for episode in range(old_total_episode + 1, total_episode + 1):
                        episode_priority.setdefault(str(episode), 0)
                    subscribe.total_episode = total_episode
                    subscribe.episode_priority = episode_priority
                    current_priority = self.get_best_version_current_priority(subscribe, episode_priority)
                logger.info(
                    f'订阅 {subscribe.name} 总集数变化，更新总集数为{total_episode}，缺失集数为{lack_episode} ...')
            else:
                total_episode = subscribe.total_episode
                lack_episode = subscribe.lack_episode
                if subscribe.best_version and subscribe.type == MediaType.TV.value:
                    current_priority = self.get_best_version_current_priority(subscribe)
            # 更新TMDB信息
            update_data = {
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
            }
            if subscribe.best_version and subscribe.type == MediaType.TV.value:
                update_data["current_priority"] = current_priority
                if not subscribe.manual_total_episode and len(episodes):
                    update_data["episode_priority"] = subscribe.episode_priority
                subscribe.current_priority = current_priority
            subscribe.total_episode = total_episode
            subscribe.lack_episode = lack_episode
            subscribeoper.update(subscribe.id, update_data)
            logger.info(f'{subscribe.name} 订阅元数据更新完成')

    def get_subscribe_by_source(self, source: str) -> Optional[Subscribe]:
        """
        从来源获取订阅
        """
        source_keyword = self.parse_subscribe_source_keyword(source)
        if not source_keyword:
            return None
        # 只保留需要的字段动态获取订阅
        valid_fields = {k: v for k, v in source_keyword.items()
                        if k in ["type", "season", "tmdbid", "doubanid", "bangumiid"]}
        # 暂时不考虑订阅历史, 若有必要再添加
        return SubscribeOper().get_by(**valid_fields)

    @staticmethod
    def follow():
        """
        刷新follow的用户分享，并自动添加订阅
        """
        follow_users: List[str] = SystemConfigOper().get(SystemConfigKey.FollowSubscribers)
        if not follow_users:
            return
        logger.info(f'开始刷新follow用户分享订阅 ...')
        success_count = 0
        subscribeoper = SubscribeOper()
        for share_sub in MoviePilotServerHelper.get_subscribe_shares():
            if global_vars.is_system_stopped:
                break
            uid = share_sub.get("share_uid")
            if uid and uid in follow_users:
                # 订阅已存在则跳过
                if subscribeoper.exists(tmdbid=share_sub.get("tmdbid"),
                                        doubanid=share_sub.get("doubanid"),
                                        season=share_sub.get("season")):
                    continue
                # 已经订阅过跳过
                if subscribeoper.exist_history(tmdbid=share_sub.get("tmdbid"),
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

    async def cache_calendar(self):
        """
        预缓存订阅日历，实际上就是查询一遍所有订阅的媒体信息
        前端请示是异常的，所以需要使用异步缓存方法
        """
        logger.info(f'开始预缓存订阅日历 ...')
        for subscribe in await SubscribeOper().async_list():
            if global_vars.is_system_stopped:
                break
            try:
                mtype = MediaType(subscribe.type)
            except ValueError:
                logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                continue
            # 识别媒体信息
            if mtype == MediaType.MOVIE:
                mediainfo: MediaInfo = await self.async_recognize_media(mtype=mtype,
                                                                        tmdbid=subscribe.tmdbid,
                                                                        doubanid=subscribe.doubanid,
                                                                        bangumiid=subscribe.bangumiid,
                                                                        episode_group=subscribe.episode_group,
                                                                        cache=False)
                if not mediainfo:
                    logger.warn(
                        f'未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}')
                    continue
            else:
                episodes = await TmdbChain().async_tmdb_episodes(tmdbid=subscribe.tmdbid,
                                                                 season=subscribe.season,
                                                                 episode_group=subscribe.episode_group)
                if not episodes:
                    logger.warn(
                        f'未识别到季集信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，豆瓣ID：{subscribe.doubanid}，季：{subscribe.season}')
                    continue
        logger.info(f'订阅日历预缓存完成')

    @staticmethod
    def __update_subscribe_note(subscribe: Subscribe, downloads: Optional[List[Context]]):
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
            SubscribeOper().update(subscribe.id, {
                "note": note
            })

    @staticmethod
    def __get_downloaded(subscribe: Subscribe) -> List[int]:
        """
        获取已下载过的集数或电影。

        洗版分支只返回 priority==100 的完成集；priority<100 的集仍要继续搜索更高
        优先级版本，不能并入返回值（会让下游把 pending 减空、订阅卡死）。
        note 由非洗版分支消费，用于洗版关闭后的迁移读取。
        """
        if subscribe.best_version:
            if subscribe.type == MediaType.TV.value:
                completed = SubscribeChain.__get_best_version_completed_episodes(subscribe)
                if completed:
                    logger.info(f'订阅 {subscribe.name} 第{subscribe.season}季 已完成洗版剧集：{completed}')
                return completed
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

    @staticmethod
    def __update_lack_episodes(lefts: Dict[Union[int, str], Dict[int, schemas.NotExistMediaInfo]],
                               subscribe: Subscribe,
                               mediainfo: MediaInfo,
                               update_date: Optional[bool] = False):
        """
        写入订阅 lack_episode，可选同时刷新 last_update。

        lack 统一语义为"订阅范围内尚未下载到任何版本的集数"。
        - 普通订阅：lack 从 ``lefts`` 提取（lefts 已在 ``__get_subscribe_no_exits`` 里扣过 note）
        - 洗版订阅：lack = ``[start, total]`` 范围内既不在 note 也不在 episode_priority(>0) 命中的集数。
          洗版的 lefts 由 ``check_and_handle_existing_media`` 按 priority<100 构造，承担"搜索目标"职责，
          与"未下载"维度并不同义——若复用会把"已下载但待升级"的集错算成 lack。
        """
        update_data = {}
        if update_date:
            update_data["last_update"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if subscribe.type == MediaType.TV.value:
            if subscribe.best_version:
                lack_episode = SubscribeChain.__compute_best_version_lack_episode(subscribe)
                logger.info(f"{mediainfo.title_year} 季 {subscribe.season} 剩余未下载剧集数为{lack_episode} ...")
            elif not lefts:
                # lefts 为空：媒体库实缺为 0
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
            SubscribeOper().update(subscribe.id, update_data)

    @staticmethod
    def __compute_best_version_lack_episode(subscribe: Subscribe) -> int:
        """
        计算洗版订阅"未下载集数"：在 ``[start, total]`` 范围内排除已在 ``note`` 或
        ``episode_priority`` (>0) 中记账的集。priority<100 但 >0 的集视为"已下载、待升级"，
        不计入 lack——与 UI 上"已下载 = total - lack"展示口径一致。
        """
        total_episode = subscribe.total_episode or 0
        if not total_episode:
            return 0
        start_episode = subscribe.start_episode or 1
        if total_episode < start_episode:
            return 0
        target_episodes = set(range(start_episode, total_episode + 1))
        downloaded: set = set()
        for ep in (subscribe.note or []):
            try:
                downloaded.add(int(ep))
            except (TypeError, ValueError):
                continue
        for ep_str, priority in (subscribe.episode_priority or {}).items():
            if not str(ep_str).isdigit():
                continue
            try:
                if float(priority) > 0:
                    downloaded.add(int(ep_str))
            except (TypeError, ValueError):
                continue
        return len(target_episodes - downloaded)

    def __finish_subscribe(self, subscribe: Subscribe, mediainfo: MediaInfo, meta: MetaBase):
        """
        完成订阅
        """
        # 如果订阅状态为待定（P），说明订阅信息尚未完全更新，无法完成订阅
        if subscribe.state == "P":
            return
        # 发送订阅完成判定事件，在写入 DB 前，允许外部据完结策略否决本次自动完成
        completion_event = eventmanager.send_event(
            ChainEventType.SubscribeCompletionCheck,
            SubscribeCompletionCheckEventData(subscribe=subscribe, mediainfo=mediainfo, meta=meta))
        if completion_event and completion_event.event_data:
            completion_data: SubscribeCompletionCheckEventData = completion_event.event_data
            if completion_data.cancel:
                logger.info(f'{mediainfo.title_year} 完成被 [{completion_data.source}] 否决：{completion_data.reason}')
                return
        # 完成订阅
        msgstr = "订阅" if not subscribe.best_version else "洗版"
        logger.info(f'{mediainfo.title_year} 完成{msgstr}')
        # 新增订阅历史
        subscribeoper = SubscribeOper()
        subscribeoper.add_history(**subscribe.to_dict())
        # 删除订阅
        subscribeoper.delete(subscribe.id)
        # 发送通知
        if mediainfo.type == MediaType.TV:
            link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
        else:
            link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
        # 完成订阅按规则发送消息
        self.post_message(
            schemas.Notification(
                mtype=NotificationType.Subscribe,
                ctype=ContentType.SubscribeComplete,
                image=mediainfo.get_message_image(),
                link=link,
                username=subscribe.username
            ),
            meta=meta,
            mediainfo=mediainfo,
            msgstr=msgstr,
            username=subscribe.username
        )
        # 发送事件
        eventmanager.send_event(EventType.SubscribeComplete, {
            "subscribe_id": subscribe.id,
            "subscribe_info": subscribe.to_dict(),
            "mediainfo": mediainfo.to_dict(),
        })
        # 统计订阅
        MoviePilotServerHelper.sub_done_async({
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id
        })

    def remote_list(
        self,
        arg_str: str = "",
        channel: MessageChannel = None,
        userid: Union[str, int] = None,
        source: Optional[str] = None,
    ):
        """
        /subscribes 统一入口。
        """
        request = subscribe_interaction_manager.create_or_replace(
            user_id=userid,
            command="/subscribes",
            channel=channel,
            source=source,
            username=None,
        )
        normalized_arg = (arg_str or "").strip()
        if normalized_arg and self.handle_text_interaction(
            channel=channel,
            source=source,
            userid=userid,
            username="",
            text=normalized_arg,
        ):
            return
        self._render_subscribe_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username="",
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str]]:
        """
        解析 /subscribes 按钮回调。
        """
        if not callback_data.startswith("subscribes:"):
            return None
        parts = callback_data.split(":")
        if len(parts) < 3:
            return None
        return parts[1], parts[2]

    def handle_callback_interaction(
        self,
        callback_data: str,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理 /subscribes 按钮交互。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action = parsed
        request = subscribe_interaction_manager.get_by_id(request_id, userid)
        if not request:
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="订阅交互已失效，请重新发送 /subscribes",
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "close":
            subscribe_interaction_manager.remove(request.request_id)
            update_or_post_message(
                chain=self,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅管理",
                text="订阅交互已结束",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "page-prev":
            request.page = max(0, request.page - 1)
            request.awaiting_input = None
        elif action == "page-next":
            request.page += 1
            request.awaiting_input = None
        elif action in {"search", "delete"}:
            request.awaiting_input = action
        elif action == "refresh":
            request.awaiting_input = None
            self._run_refresh_action(channel, source, userid, username)
        elif action == "refresh-list":
            request.awaiting_input = None
        elif action == "metadata":
            request.awaiting_input = None
            self._run_metadata_refresh_action(channel, source, userid, username)

        self._render_subscribe_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        return True

    def handle_text_interaction(
        self,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
        text: str,
    ) -> bool:
        """
        处理 /subscribes 文本补充输入。
        """
        request = subscribe_interaction_manager.get_by_user(userid)
        if not request:
            return False

        request.channel = channel
        request.source = source
        request.username = username

        normalized = (text or "").strip()
        lowered = normalized.lower()

        if lowered in {"退出", "关闭", "q", "quit", "exit"}:
            subscribe_interaction_manager.remove(request.request_id)
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="订阅交互已结束",
                )
            )
            return True

        if lowered in {"取消", "cancel", "返回", "back"}:
            request.awaiting_input = None
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新列表", "列表", "list"}:
            request.awaiting_input = None
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新", "refresh"}:
            request.awaiting_input = None
            self._run_refresh_action(channel, source, userid, username)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"元数据", "刷新元数据", "metadata"}:
            request.awaiting_input = None
            self._run_metadata_refresh_action(channel, source, userid, username)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"p", "prev", "上一页"}:
            request.awaiting_input = None
            request.page = max(0, request.page - 1)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"n", "next", "下一页"}:
            request.awaiting_input = None
            request.page += 1
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        search_match = re.match(r"^(?:搜索|search)\s+(.+)$", normalized, re.IGNORECASE)
        delete_match = re.match(r"^(?:删除|delete)\s+(.+)$", normalized, re.IGNORECASE)

        if request.awaiting_input == "search":
            success, message = self._run_search_action(
                normalized, channel, source, userid, username
            )
            request.awaiting_input = None
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.awaiting_input == "delete":
            success, message = self._delete_subscribes(normalized)
            request.awaiting_input = None
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if search_match:
            success, message = self._run_search_action(
                search_match.group(1), channel, source, userid, username
            )
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if delete_match:
            success, message = self._delete_subscribes(delete_match.group(1))
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        self.post_message(
            schemas.Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=self._subscribe_usage_hint(request.awaiting_input),
            )
        )
        return True

    def _render_subscribe_interaction(
        self,
        request,
        channel: MessageChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> None:
        """
        渲染 /subscribes 当前页面。
        """
        subscribes = SubscribeOper().list()
        page_size = (
            self._button_page_size
            if supports_interaction_buttons(channel)
            else self._text_page_size
        )
        page_subscribes, page, total_pages = page_items(
            subscribes, request.page, page_size
        )
        request.page = page

        if subscribes:
            body = self._format_subscribe_list(page_subscribes, channel=channel)
            footer = [
                f"第 {page + 1}/{total_pages} 页，共 {len(subscribes)} 个订阅",
                self._subscribe_prompt(request.awaiting_input),
                self._subscribe_usage_hint(request.awaiting_input),
            ]
            text = "\n\n".join([body, *[line for line in footer if line]])
        else:
            text = "当前没有任何订阅。\n\n输入 `退出` 结束交互。"

        buttons = None
        if supports_interaction_buttons(channel):
            buttons = build_navigation_buttons(
                "subscribes", request, page, total_pages
            )
            buttons.extend(
                [
                    [
                        {
                            "text": "搜索订阅",
                            "callback_data": f"subscribes:{request.request_id}:search",
                        },
                        {
                            "text": "删除订阅",
                            "callback_data": f"subscribes:{request.request_id}:delete",
                        },
                        {
                            "text": "刷新订阅",
                            "callback_data": f"subscribes:{request.request_id}:refresh",
                        },
                    ],
                    [
                        {
                            "text": "刷新元数据",
                            "callback_data": f"subscribes:{request.request_id}:metadata",
                        },
                        {
                            "text": "刷新列表",
                            "callback_data": f"subscribes:{request.request_id}:refresh-list",
                        },
                        {
                            "text": "关闭",
                            "callback_data": f"subscribes:{request.request_id}:close",
                        },
                    ],
                ]
            )

        update_or_post_message(
            chain=self,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="订阅管理",
            text=text,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _format_subscribe_list(
        self, subscribes: List[Subscribe], channel: Optional[MessageChannel]
    ) -> str:
        """
        根据渠道能力格式化订阅列表。
        """
        if supports_markdown(channel):
            rows = [
                [
                    subscribe.id,
                    subscribe.name,
                    subscribe.type,
                    subscribe.year or "-",
                    self._format_subscribe_progress(subscribe),
                    self._format_subscribe_state(subscribe.state),
                ]
                for subscribe in subscribes
            ]
            return format_markdown_table(
                headers=["ID", "名称", "类型", "年份", "季/进度", "状态"],
                rows=rows,
            )

        lines = []
        for subscribe in subscribes:
            lines.append(
                f"{subscribe.id}. {subscribe.name}（{subscribe.year or '-'}）"
                f" | {subscribe.type}"
                f" | {self._format_subscribe_progress(subscribe)}"
                f" | 状态：{self._format_subscribe_state(subscribe.state)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_subscribe_state(state: Optional[str]) -> str:
        """
        订阅状态显示文本。
        """
        mapping = {
            "N": "新建",
            "R": "订阅中",
            "P": "待定",
            "S": "暂停",
        }
        return mapping.get(state or "", state or "-")

    @staticmethod
    def _format_subscribe_progress(subscribe: Subscribe) -> str:
        """
        构造订阅的季和进度说明。
        """
        if subscribe.type == MediaType.MOVIE.value:
            return "电影"
        season = subscribe.season or 1
        if subscribe.total_episode:
            lack_episode = (
                subscribe.lack_episode
                if subscribe.lack_episode is not None
                else subscribe.total_episode
            )
            downloaded = max(subscribe.total_episode - lack_episode, 0)
            return f"第{season}季 [{downloaded}/{subscribe.total_episode}]"
        return f"第{season}季"

    @staticmethod
    def _subscribe_prompt(awaiting_input: Optional[str]) -> str:
        """
        返回当前输入模式提示。
        """
        if awaiting_input == "search":
            return "当前操作：搜索订阅，请输入订阅 ID，多个 ID 用空格分隔，或输入 all 搜索全部。"
        if awaiting_input == "delete":
            return "当前操作：删除订阅，请输入订阅 ID，多个 ID 用空格分隔。"
        return ""

    @staticmethod
    def _subscribe_usage_hint(awaiting_input: Optional[str]) -> str:
        """
        返回 /subscribes 的文本操作提示。
        """
        if awaiting_input == "search":
            return "输入订阅 ID 或 all；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        if awaiting_input == "delete":
            return "输入一个或多个订阅 ID；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        return (
            "可输入：`搜索 <id...|all>`、`删除 <id...>`、`刷新`、`刷新元数据`、`n`、`p`、`退出`。"
        )

    def _run_refresh_action(
        self,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
    ) -> None:
        """
        执行订阅刷新。
        """
        self.post_message(
            schemas.Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="开始刷新订阅...",
            )
        )
        self.refresh()
        self.post_message(
            schemas.Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅刷新执行完成",
            )
        )

    def _run_metadata_refresh_action(
        self,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
    ) -> None:
        """
        执行订阅元数据刷新。
        """
        self.post_message(
            schemas.Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="开始刷新订阅元数据...",
            )
        )
        self.check()
        self.post_message(
            schemas.Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅元数据刷新完成",
            )
        )

    @staticmethod
    def _parse_subscribe_ids(arg_str: str) -> List[int]:
        """
        从输入中提取订阅 ID。
        """
        return [int(item) for item in re.findall(r"\d+", arg_str or "")]

    def _run_search_action(
        self,
        arg_str: str,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
    ) -> Tuple[bool, str]:
        """
        手动执行订阅搜索。
        """
        normalized = (arg_str or "").strip()
        if not normalized or normalized.lower() in {"all", "全部", "所有"}:
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="开始搜索所有订阅...",
                )
            )
            self.search(state="N,R,P", manual=True)
            return True, "所有订阅搜索完成"

        subscribe_ids = self._parse_subscribe_ids(normalized)
        if not subscribe_ids:
            return False, "请输入订阅 ID，多个 ID 用空格分隔，或输入 all"

        subscribeoper = SubscribeOper()
        missing = []
        searched = []
        for subscribe_id in subscribe_ids:
            subscribe = subscribeoper.get(subscribe_id)
            if not subscribe:
                missing.append(str(subscribe_id))
                continue
            self.post_message(
                schemas.Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"开始搜索订阅【{subscribe.name}】...",
                )
            )
            self.search(sid=subscribe_id, manual=True)
            searched.append(subscribe.name)

        if not searched and missing:
            return False, f"未找到订阅：{', '.join(missing)}"

        message = f"已完成 {len(searched)} 个订阅搜索"
        if searched:
            message += f"：{', '.join(searched)}"
        if missing:
            message += f"；未找到：{', '.join(missing)}"
        return True, message

    def _delete_subscribes(self, arg_str: str) -> Tuple[bool, str]:
        """
        批量删除订阅。
        """
        subscribe_ids = self._parse_subscribe_ids(arg_str)
        if not subscribe_ids:
            return False, "请输入至少一个有效的订阅 ID"

        subscribeoper = SubscribeOper()
        deleted = []
        missing = []
        for subscribe_id in subscribe_ids:
            subscribe = subscribeoper.get(subscribe_id)
            if not subscribe:
                missing.append(str(subscribe_id))
                continue
            deleted.append(subscribe.name)
            subscribeoper.delete(subscribe_id)
            MoviePilotServerHelper.sub_done_async(
                {
                    "tmdbid": subscribe.tmdbid,
                    "doubanid": subscribe.doubanid,
                }
            )

        if not deleted and missing:
            return False, f"未找到订阅：{', '.join(missing)}"

        message = f"已删除 {len(deleted)} 个订阅"
        if deleted:
            message += f"：{', '.join(deleted)}"
        if missing:
            message += f"；未找到：{', '.join(missing)}"
        return True, message

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
        subscribeoper = SubscribeOper()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            subscribe_id = int(arg_str)
            subscribe = subscribeoper.get(subscribe_id)
            if not subscribe:
                self.post_message(schemas.Notification(channel=channel, source=source,
                                                       title=f"订阅编号 {subscribe_id} 不存在！", userid=userid))
                return
            # 删除订阅
            subscribeoper.delete(subscribe_id)
            # 统计订阅
            MoviePilotServerHelper.sub_done_async({
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
                    # 交集为空时，说明订阅的剧集均已入库
                    if not episodes:
                        return True, {}
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
        subscribeoper = SubscribeOper()
        if site_id == "*":
            # 站点被重置
            SystemConfigOper().set(SystemConfigKey.RssSites, [])
            for subscribe in subscribeoper.list():
                if not subscribe.sites:
                    continue
                subscribeoper.update(subscribe.id, {
                    "sites": []
                })
            return
        # 从选中的rss站点中移除
        selected_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []
        if site_id in selected_sites:
            selected_sites.remove(site_id)
            SystemConfigOper().set(SystemConfigKey.RssSites, selected_sites)
        # 查询所有订阅
        for subscribe in subscribeoper.list():
            if not subscribe.sites:
                continue
            sites = subscribe.sites or []
            if site_id not in sites:
                continue
            sites.remove(site_id)
            subscribeoper.update(subscribe.id, {
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

    @staticmethod
    def get_params(subscribe: Subscribe):
        """
        获取订阅默认参数
        """
        # 默认过滤规则
        default_rule = SystemConfigOper().get(SystemConfigKey.SubscribeDefaultParams) or {}
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
            tmdb_episodes = TmdbChain().tmdb_episodes(
                tmdbid=subscribe.tmdbid,
                season=subscribe.season,
                episode_group=subscribe.episode_group
            )
            if tmdb_episodes:
                for episode in tmdb_episodes:
                    info = schemas.SubscribeEpisodeInfo()
                    info.title = episode.name
                    info.description = episode.overview
                    info.backdrop = settings.TMDB_IMAGE_URL(episode.still_path, "w500")
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
        downloadhis = DownloadHistoryOper()
        download_his = downloadhis.get_by_mediaid(tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid)
        if download_his:
            for his in download_his:
                # 查询下载文件
                files = downloadhis.get_files_by_hash(his.download_hash, state=1)
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
                            if season_number is not None and season_number != subscribe.season:
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
                    if season_number is not None and season_number != subscribe.season:
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
        self.__refresh_total_episode_before_completion(subscribe=subscribe, mediainfo=mediainfo)

        # 非洗版
        if not subscribe.best_version:
            # 每季总集数
            totals = {}
            if subscribe.season and subscribe.total_episode:
                totals = {
                    subscribe.season: subscribe.total_episode
                }
            # 查询媒体库缺失的媒体信息
            exist_flag, no_exists = DownloadChain().get_no_exists_info(
                meta=meta,
                mediainfo=mediainfo,
                totals=totals
            )
        else:
            # 洗版，如果已经满足了优先级，则认为已经洗版完成
            if self.__is_best_version_complete(subscribe):
                exist_flag = True
                no_exists = {}
            else:
                exist_flag = False
                if meta.type == MediaType.TV:
                    pending_episodes = [] if self.__is_full_best_version_enabled(
                        subscribe
                    ) else self._get_pending_best_version_episodes(subscribe)
                    # 对于电视剧，构造缺失的媒体信息
                    no_exists = {
                        mediakey: {
                            subscribe.season: schemas.NotExistMediaInfo(
                                season=subscribe.season,
                                episodes=pending_episodes,
                                total_episode=subscribe.total_episode,
                                start_episode=subscribe.start_episode or 1,
                                # 完整覆盖约束会影响整季文件探测、显式集列表匹配和多集拆包降级。
                                require_complete_coverage=self.__is_full_best_version_enabled(subscribe))
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
        if self.__is_full_best_version_enabled(subscribe):
            # 全集洗版必须保留整季缺失范围，避免下载链路从整包中拆选单集。
            downloaded = []
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
    def __apply_episodes_refresh(current_total: int, season: Optional[int], *,
                                 mediainfo: Optional[MediaInfo] = None,
                                 tmdbid: Optional[int] = None,
                                 doubanid: Optional[str] = None,
                                 subscribe_id: Optional[int] = None,
                                 scene: Optional[str] = None) -> int:
        """
        发送订阅总集数推算事件，允许外部据自身策略覆盖按 TMDB 季集数算出的总集数。

        用途：插件在"待定集数"等场景经事件注入 total_episode
        无监听者或外部未覆盖时返回入参原值，保证零行为变更。
        :param current_total: 主程序按 TMDB 季集数算出的默认总集数
        :param season: 季号
        :return: 最终采用的总集数
        """
        event_data = SubscribeEpisodesRefreshEventData(
            tmdbid=tmdbid, doubanid=doubanid, season=season, mediainfo=mediainfo,
            current_total_episode=current_total, subscribe_id=subscribe_id, scene=scene)
        event = eventmanager.send_event(ChainEventType.SubscribeEpisodesRefresh, event_data)
        if event and event.event_data:
            result: SubscribeEpisodesRefreshEventData = event.event_data
            if result.updated and result.total_episode:
                return result.total_episode
        return current_total

    @staticmethod
    async def __async_apply_episodes_refresh(current_total: int, season: Optional[int], *,
                                             mediainfo: Optional[MediaInfo] = None,
                                             tmdbid: Optional[int] = None,
                                             doubanid: Optional[str] = None,
                                             subscribe_id: Optional[int] = None,
                                             scene: Optional[str] = None) -> int:
        """
        __apply_episodes_refresh 的异步版本
        """
        event_data = SubscribeEpisodesRefreshEventData(
            tmdbid=tmdbid, doubanid=doubanid, season=season, mediainfo=mediainfo,
            current_total_episode=current_total, subscribe_id=subscribe_id, scene=scene)
        event = await eventmanager.async_send_event(ChainEventType.SubscribeEpisodesRefresh, event_data)
        if event and event.event_data:
            result: SubscribeEpisodesRefreshEventData = event.event_data
            if result.updated and result.total_episode:
                return result.total_episode
        return current_total

    def __refresh_total_episode_before_completion(self, subscribe: Subscribe, mediainfo: MediaInfo):
        """
        在完成判断前，按最新识别结果兜底修正订阅总集数，防止旧总集数导致误完成。
        """
        if subscribe.type != MediaType.TV.value:
            return
        if subscribe.manual_total_episode:
            return
        if subscribe.season is None:
            return

        new_total_episode = len((mediainfo.seasons or {}).get(subscribe.season) or [])
        # 允许外部覆盖按 TMDB 算出的总集数（如待定集数），后续“只增不减”仍作用于覆盖后的结果，避免误减导致提前完成。
        new_total_episode = self.__apply_episodes_refresh(
            new_total_episode, season=subscribe.season, mediainfo=mediainfo,
            tmdbid=subscribe.tmdbid, doubanid=subscribe.doubanid,
            subscribe_id=subscribe.id, scene="precheck")
        old_total_episode = subscribe.total_episode or 0
        if not new_total_episode or new_total_episode <= old_total_episode:
            return

        old_lack_episode = subscribe.lack_episode or 0
        new_lack_episode = old_lack_episode + (new_total_episode - old_total_episode)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SubscribeOper().update(subscribe.id, {
            "total_episode": new_total_episode,
            "lack_episode": new_lack_episode,
            "last_update": now
        })
        subscribe.total_episode = new_total_episode
        subscribe.lack_episode = new_lack_episode
        subscribe.last_update = now
        logger.info(
            f"订阅 {subscribe.name} 第{subscribe.season}季 总集数更新为 {new_total_episode}，缺失集数更新为 {new_lack_episode}"
        )

    @classmethod
    def _is_episode_range_covered(cls, meta: MetaBase, subscribe: Subscribe) -> bool:
        """
        判断种子是否覆盖当前仍需洗版的剧集范围。
        """
        episodes = meta.episode_list
        if not episodes:
            # 没有剧集信息，表示该种子为合集
            return True

        pending_episodes = cls._get_pending_best_version_episodes(subscribe)
        if not pending_episodes:
            return True

        return bool(set(episodes).intersection(set(pending_episodes)))

    @staticmethod
    def __get_media_id_match_source(mediainfo: Optional[MediaInfo]) -> str:
        """
        返回候选自身识别命中的明确媒体 ID 类型。
        """
        if mediainfo and mediainfo.tmdb_id:
            return "tmdbid"
        if mediainfo and mediainfo.douban_id:
            return "doubanid"
        return "unknown"

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
        :return str: 格式化的订阅来源关键字字符串，格式为 "Subscribe|{...}"
        """
        source_keyword = {
            'id': subscribe.id,
            'name': subscribe.name,
            'year': subscribe.year,
            'type': subscribe.type,
            'season': subscribe.season,
            'episode_group': subscribe.episode_group,
            'tmdbid': subscribe.tmdbid,
            'imdbid': subscribe.imdbid,
            'tvdbid': subscribe.tvdbid,
            'doubanid': subscribe.doubanid,
            'bangumiid': subscribe.bangumiid
        }
        return f"Subscribe|{json.dumps(source_keyword, ensure_ascii=False)}"

    @staticmethod
    def parse_subscribe_source_keyword(source_keyword_str: str) -> Optional[dict]:
        """
        解析订阅来源关键字字符串

        :param source_keyword_str: 订阅来源关键字字符串，格式为 "Subscribe|{...}"
        :return Dict: 如果解析失败则返回None
        """
        if not source_keyword_str or not source_keyword_str.startswith("Subscribe|"):
            return None

        try:
            # 分割字符串获取JSON部分
            json_part = source_keyword_str.split("|", 1)[1]
            # 解析JSON字符串
            source_keyword = json.loads(json_part)
            return source_keyword
        except (IndexError, json.JSONDecodeError, TypeError) as e:
            logger.error(f"解析订阅来源关键字失败: {e}")
            return None
