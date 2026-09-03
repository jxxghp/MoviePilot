"""订阅查询、关注与现存媒体投影"""

from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

from app.application.configuration import get_configured_system_config
from app.application.mediaserver import MediaServerHelper
from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionRepository,
    SubscriptionSnapshot,
    build_subscribe_meta,
)
from app.chain.media import MediaChain
from app.chain.mediaserver import MediaServerChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.chain.subscribe.notify import _subscription_share_snapshot
from app.chain.tmdb import TmdbChain
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import normalize_media_source
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.subscribe import SubscrbieInfo as _SchemaSubscrbieInfo
from app.schemas.subscribe import Subscribe as _SchemaSubscribe
from app.schemas.subscribe import SubscribeDownloadFileInfo as _SchemaSubscribeDownloadFileInfo
from app.schemas.subscribe import SubscribeEpisodeInfo as _SchemaSubscribeEpisodeInfo
from app.schemas.subscribe import SubscribeLibraryFileInfo as _SchemaSubscribeLibraryFileInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
    SystemConfigKey,
)


class SubscribeQueryOwner(_SubscribeOwnerBase):
    """订阅查询、关注与现存媒体投影，作为 SubscribeChain 的单一职责实现 owner。"""

    @staticmethod
    def get_sub_sites(subscribe: SubscriptionSnapshot) -> List[int]:
        """
        获取订阅中涉及的站点清单
        :param subscribe: 订阅信息对象
        :return: 涉及的站点清单
        """
        # 从系统配置获取默认订阅站点
        default_sites = get_configured_system_config().get(SystemConfigKey.RssSites) or []
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
        subscribes = self.subscription_repository.list()
        if not subscribes:
            # 没有订阅
            return None
        # 刷新订阅选中的Rss站点
        for subscribe in subscribes:
            # 刷新选中的站点
            if subscribe.state in self.get_states_for_search("R"):
                ret_sites.extend(self.get_sub_sites(subscribe))
        # 去重
        if ret_sites:
            ret_sites = list(set(ret_sites))

        return ret_sites

    def has_music_subscribe(self) -> bool:
        """判断是否存在可搜索状态的音乐订阅，用于决定是否额外刷新站点音乐入口。"""
        return cast(bool, self._subscription_query().has_music(self.get_states_for_search("R")))

    def get_subscribe_by_source(self, source: str) -> Optional[SubscriptionSnapshot]:
        """
        从来源获取订阅
        """
        return cast(
            Optional[SubscriptionSnapshot],
            self._subscription_query().get_by_source(self.parse_subscribe_source_keyword(source)),
        )

    @classmethod
    def follow(
        cls,
        progress_callback: Optional[Callable[..., None]] = None,
        repository: Optional[SubscriptionRepository] = None,
    ) -> None:
        """
        刷新follow的用户分享，并自动添加订阅

        :param progress_callback: 定时服务进度更新回调
        """
        self = cls()
        if repository is not None:
            self.subscription_repository = repository
        follow_users: List[str] = get_configured_system_config().get(SystemConfigKey.FollowSubscribers)
        if not follow_users:
            if progress_callback:
                progress_callback(value=100, text="未配置 Follow 订阅用户，跳过刷新")
            return
        logger.info("开始刷新follow用户分享订阅 ...")
        success_count = 0
        repository = self.subscription_repository
        share_subscribes = _subscription_share_snapshot().list_shares() or []
        total_num = len(share_subscribes)
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始刷新 Follow 订阅分享，共 {total_num} 条 ...",
                data={"total": total_num, "finished": 0},
            )
        for index, share_sub in enumerate(share_subscribes, start=1):
            if runtime_stop_state.is_system_stopped:
                break
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_num * 100 if total_num else 100,
                    text=f"正在处理 Follow 订阅分享（{index}/{total_num}）...",
                    data={"total": total_num, "finished": index - 1},
                )
            uid = share_sub.get("share_uid")
            if uid and uid in follow_users:
                # 订阅已存在则跳过
                media_source = normalize_media_source(share_sub.get("media_source"))
                media_id = str(share_sub.get("media_id") or "").strip()
                if not media_source or not media_id:
                    continue
                identity = SubscriptionIdentity(
                    media_source=media_source,
                    media_id=media_id,
                    type=share_sub.get("type"),
                    music_type=share_sub.get("music_type"),
                    season=share_sub.get("season"),
                    episode_group=share_sub.get("episode_group"),
                )
                if repository.exists(identity):
                    continue
                # 已经订阅过跳过
                if repository.history_exists(identity):
                    continue
                # 去除无效属性
                for key in list(share_sub.keys()):
                    if not hasattr(_SchemaSubscribe(), key):
                        share_sub.pop(key)
                # 类型转换
                subscribe_in = _SchemaSubscribe(**share_sub)
                mtype = MediaType(subscribe_in.type)
                # 非 TMDB 标题可能携带季号，入库前统一拆分。
                if mtype != MediaType.MUSIC and normalize_media_source(subscribe_in.media_source) not in (
                    None,
                    MediaSource.TMDB,
                ):
                    meta = MetaInfo(subscribe_in.name)
                    subscribe_in.name = meta.name
                    if subscribe_in.season is None:
                        subscribe_in.season = meta.begin_season
                # 标题转换
                if subscribe_in.name:
                    title = subscribe_in.name
                else:
                    title = None
                sid, message = type(self)().add(
                    mtype=mtype,
                    title=title,
                    year=subscribe_in.year,
                    season=subscribe_in.season,
                    episode_group=subscribe_in.episode_group,
                    media_source=subscribe_in.media_source,
                    media_id=subscribe_in.media_id,
                    music_type=subscribe_in.music_type,
                    total_tracks=subscribe_in.total_tracks,
                    username="订阅分享",
                    best_version=subscribe_in.best_version,
                    save_path=subscribe_in.save_path,
                    search_imdbid=subscribe_in.search_imdbid,
                    custom_words=subscribe_in.custom_words,
                    media_category_id=subscribe_in.media_category_id,
                    media_category=subscribe_in.media_category,
                    filter_groups=subscribe_in.filter_groups,
                    exist_ok=True,
                )
                if sid:
                    success_count += 1
                    logger.info(f"follow用户分享订阅 {title} 添加成功")
                else:
                    logger.error(f"follow用户分享订阅 {title} 添加失败：{message}")
        logger.info(f"follow用户分享订阅刷新完成，共添加 {success_count} 个订阅")
        if progress_callback:
            progress_callback(
                value=100,
                text=f"Follow 订阅分享刷新完成，新增 {success_count} 个订阅",
                data={
                    "total": total_num,
                    "finished": total_num,
                    "added": success_count,
                },
            )

    @staticmethod
    def _SubscribeChain__get_default_subscribe_config(mtype: MediaType, default_config_key: str) -> Optional[str]:
        """
        获取默认订阅配置
        """
        default_subscribe_key = None
        if mtype == MediaType.TV:
            default_subscribe_key = SystemConfigKey.DefaultTvSubscribeConfig.value
        if mtype == MediaType.MOVIE:
            default_subscribe_key = SystemConfigKey.DefaultMovieSubscribeConfig.value
        if mtype == MediaType.MUSIC:
            default_subscribe_key = SystemConfigKey.DefaultMusicSubscribeConfig.value

        if not default_subscribe_key:
            return None

        # 默认订阅规则属于持久化用户配置，不再从部署 Settings 猜测同名属性。
        value = get_configured_system_config().get(default_subscribe_key)

        if not value:
            return None
        return value.get(default_config_key) or None

    @staticmethod
    def get_params(subscribe: SubscriptionSnapshot) -> dict[str, Any]:
        """
        获取订阅默认参数
        """
        # 默认过滤规则
        default_rule = get_configured_system_config().get(SystemConfigKey.SubscribeDefaultParams) or {}
        return {
            key: value
            for key, value in {
                "include": subscribe.include or default_rule.get("include"),
                "exclude": subscribe.exclude or default_rule.get("exclude"),
                "quality": subscribe.quality or default_rule.get("quality"),
                "resolution": subscribe.resolution or default_rule.get("resolution"),
                "effect": subscribe.effect or default_rule.get("effect"),
                "audio_quality": getattr(subscribe, "audio_quality", None),
                "audio_format": getattr(subscribe, "audio_format", None),
                "min_bitrate": getattr(subscribe, "min_bitrate", None),
                "min_bit_depth": getattr(subscribe, "min_bit_depth", None),
                "min_sample_rate": getattr(subscribe, "min_sample_rate", None),
                "tv_size": default_rule.get("tv_size"),
                "movie_size": default_rule.get("movie_size"),
                "min_seeders": default_rule.get("min_seeders"),
                "min_seeders_time": default_rule.get("min_seeders_time"),
            }.items()
            if value is not None
        }

    def subscribe_files_info(self, subscribe: SubscriptionSnapshot) -> Optional[_SchemaSubscrbieInfo]:
        """
        订阅相关的下载和文件信息
        """
        if not subscribe:
            return None

        # 返回订阅数据
        subscribe_info = _SchemaSubscrbieInfo()

        # 所有集的数据
        episodes: Dict[int, _SchemaSubscribeEpisodeInfo] = {}
        if (
            subscribe.media_source == MediaSource.TMDB.value
            and subscribe.media_id
            and str(subscribe.media_id).isdigit()
            and subscribe.type == MediaType.TV.value
        ):
            # 查询TMDB中的集信息
            tmdb_episodes = TmdbChain().tmdb_episodes(
                tmdbid=int(subscribe.media_id), season=subscribe.season, episode_group=subscribe.episode_group
            )
            if tmdb_episodes:
                for episode in tmdb_episodes:
                    info = _SchemaSubscribeEpisodeInfo()
                    info.title = episode.name
                    info.description = episode.overview
                    info.backdrop = self.runtime_config.tmdb_image_url(episode.still_path, "w500")
                    episodes[episode.episode_number] = info
        elif subscribe.type == MediaType.TV.value:
            # 根据开始结束集计算集信息
            for i in range(subscribe.start_episode or 1, subscribe.total_episode + 1):
                info = _SchemaSubscribeEpisodeInfo()
                info.title = f"第 {i} 集"
                episodes[i] = info
        else:
            # 电影
            info = _SchemaSubscribeEpisodeInfo()
            info.title = subscribe.name
            episodes[0] = info

        # 所有下载记录
        downloadhis = self.download_history_repository
        download_his = []
        if subscribe.media_source and subscribe.media_id:
            download_his = downloadhis.get_by_media_identity(
                media_source=subscribe.media_source,
                media_id=subscribe.media_id,
                music_type=getattr(subscribe, "music_type", None),
            )
        if download_his:
            for his in download_his:
                if not his.download_hash:
                    continue
                # 查询下载文件
                files = downloadhis.get_files_by_hash(his.download_hash, state=1)
                if files:
                    for file in files:
                        if not file.filepath:
                            continue
                        # 识别文件名
                        file_meta = MetaInfo(file.filepath)
                        # 下载文件信息
                        file_info = _SchemaSubscribeDownloadFileInfo(
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

        try:
            meta = build_subscribe_meta(subscribe)
        except ValueError:
            logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}")
            return subscribe_info
        # 识别媒体信息
        mediainfo: MediaInfo = MediaChain().recognize_media(
            meta=meta,
            mtype=meta.type,
            **subscribe_recognize_kwargs(subscribe),
            episode_group=subscribe.episode_group,
            cache=False,
        )
        if not mediainfo:
            logger.warn(
                f"未识别到媒体信息，标题：{subscribe.name}，"
                f"媒体来源：{subscribe.media_source}，媒体 ID：{subscribe.media_id}"
            )
            return subscribe_info

        # 所有媒体库文件记录
        library_fileitems = self.media_files(mediainfo)
        if library_fileitems:
            for fileitem in library_fileitems:
                # 识别文件名
                file_meta = MetaInfo(fileitem.path)
                # 媒体库文件信息
                file_info = _SchemaSubscribeLibraryFileInfo(
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

        self._append_subscribe_media_servers(subscribe, mediainfo, episodes)

        # 更新订阅信息
        subscribe_info.subscribe = _SchemaSubscribe(**subscribe.to_dict())
        subscribe_info.episodes = episodes
        return subscribe_info

    def _append_subscribe_media_servers(
        self,
        subscribe: SubscriptionSnapshot,
        mediainfo: MediaInfo,
        episodes: Dict[int, _SchemaSubscribeEpisodeInfo],
    ) -> None:
        """合并媒体服务器条目，跳过已经由本地媒体库记录覆盖的服务。"""
        mediaserver_chain = MediaServerChain()
        server_names = list(MediaServerHelper().get_services().keys())

        def has_server_entry(
            library_list: List[_SchemaSubscribeLibraryFileInfo],
            server_name: Optional[str],
            server_type: Optional[str],
        ) -> bool:
            """判断媒体库列表是否已经包含目标服务条目。"""
            for info in library_list or []:
                if info.server and server_name and info.server == server_name:
                    return True
                if (
                    info.server_type
                    and server_type
                    and info.server_type == server_type
                    and info.server == server_name
                    and (not info.file_path or str(info.file_path).startswith(("http://", "https://")))
                ):
                    return True
            return False

        for server_name in server_names:
            exists_media = self.media_exists(mediainfo=mediainfo, server=server_name)
            if not exists_media or not (exists_media.server or exists_media.server_type):
                continue
            resolved_server = exists_media.server or server_name
            server_storage = exists_media.server_type or resolved_server
            server_itemid = str(exists_media.itemid) if exists_media.itemid is not None else None
            series_detail_url = None
            if resolved_server and exists_media.itemid is not None:
                series_detail_url = mediaserver_chain.get_play_url(
                    server=resolved_server,
                    item_id=exists_media.itemid,
                )

            if subscribe.type != MediaType.TV.value:
                episode_info = episodes.get(0)
                if episode_info and not has_server_entry(
                    episode_info.library, resolved_server, exists_media.server_type
                ):
                    episode_info.library.append(
                        _SchemaSubscribeLibraryFileInfo(
                            storage=server_storage,
                            file_path=series_detail_url,
                            server=resolved_server,
                            server_type=exists_media.server_type,
                            itemid=server_itemid,
                        )
                    )
                continue

            season_number = subscribe.season if subscribe.season is not None else 1
            exist_episodes = (exists_media.seasons or {}).get(season_number) or []
            episode_item_ids: Dict[int, str] = {}
            if resolved_server and exists_media.itemid is not None:
                episode_item_ids = mediaserver_chain.get_season_episode_ids(
                    server=resolved_server,
                    item_id=exists_media.itemid,
                    season=season_number,
                )
            for episode_number in exist_episodes:
                episode_info = episodes.get(episode_number)
                if not episode_info or has_server_entry(
                    episode_info.library, resolved_server, exists_media.server_type
                ):
                    continue
                episode_itemid = episode_item_ids.get(episode_number) or server_itemid
                detail_url = series_detail_url
                if resolved_server and episode_item_ids.get(episode_number):
                    detail_url = (
                        mediaserver_chain.get_play_url(
                            server=resolved_server,
                            item_id=episode_itemid,
                        )
                        or series_detail_url
                    )
                episode_info.library.append(
                    _SchemaSubscribeLibraryFileInfo(
                        storage=server_storage,
                        file_path=detail_url,
                        server=resolved_server,
                        server_type=exists_media.server_type,
                        itemid=(str(episode_itemid) if episode_itemid is not None else None),
                    )
                )

    def check_and_handle_existing_media(
        self, subscribe: SubscriptionSnapshot, meta: MetaBase, mediainfo: MediaInfo, mediakey: Union[str, int]
    ) -> Tuple[bool, Dict[Union[str, int], Dict[int, _SchemaNotExistMediaInfo]]]:
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
        subscribe = self._SubscribeChain__refresh_total_episode_before_completion(
            subscribe=subscribe,
            mediainfo=mediainfo,
            meta=meta,
            mediakey=mediakey,
        )

        exist_flag, no_exists = self.resolve_subscribe_missing(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=mediakey,
        )

        # 如果已下载完毕，执行订阅完成操作
        if exist_flag:
            logger.info(f"{mediainfo.title_year} 已全部下载")
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, force=True)
            return True, no_exists

        # 返回结果，表示媒体未完全下载或存在
        return False, no_exists
