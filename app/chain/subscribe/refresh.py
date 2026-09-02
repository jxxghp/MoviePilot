"""订阅元数据、进度与剧集范围刷新"""

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

from app.application.subscription import priority as _priority
from app.application.subscription.candidates import CandidateBatch
from app.application.subscription.contract import (
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
    subscribe_media_keys,
)
from app.application.subscription.facts import FreshFactLease
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.chain.subscribe.metadata import SubscribeMetadataOwner
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.event import SubscribeEpisodesRefreshEventData
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.types import (
    ChainEventType,
    MediaSource,
    MediaType,
)


class SubscribeRefreshOwner(SubscribeMetadataOwner):
    """订阅元数据、进度与剧集范围刷新，作为 SubscribeChain 的单一职责实现 owner。"""

    def refresh(self, progress_callback: Optional[Callable[..., None]] = None) -> None:
        """
        订阅刷新

        :param progress_callback: 定时服务进度更新回调
        """
        # 触发刷新站点资源，从缓存中匹配订阅
        sites = self.get_subscribed_sites()
        if sites is None:
            if progress_callback:
                progress_callback(value=100, text="没有订阅需要刷新")
            return

        def _update_refresh_progress(
            value: Optional[float] = None,
            text: Optional[str] = None,
            data: Optional[dict[str, Any]] = None,
        ) -> None:
            """将站点刷新进度映射到订阅刷新的前半阶段。"""
            if progress_callback:
                progress_callback(
                    value=(value or 0) * 0.6,
                    text=text,
                    data=data,
                )

        def _update_match_progress(
            value: Optional[float] = None,
            text: Optional[str] = None,
            data: Optional[dict[str, Any]] = None,
        ) -> None:
            """将订阅匹配进度映射到订阅刷新的后半阶段。"""
            if progress_callback:
                progress_callback(
                    value=60 + (value or 0) * 0.4,
                    text=text,
                    data=data,
                )

        torrents_chain = TorrentsChain()
        candidate_batch = torrents_chain.refresh_batch(
            sites=sites,
            progress_callback=_update_refresh_progress if progress_callback else None,
            # 存在音乐订阅时额外抓取站点音乐专用入口，音乐不一定在默认种子首页
            include_music=self.has_music_subscribe(),
        )
        if not isinstance(candidate_batch, CandidateBatch):
            legacy_candidates = torrents_chain.refresh(
                sites=sites,
                progress_callback=_update_refresh_progress if progress_callback else None,
                include_music=self.has_music_subscribe(),
            )
            candidate_batch = CandidateBatch.from_legacy(legacy_candidates or {}, source="refresh")
        self.match_batch(
            candidate_batch,
            progress_callback=_update_match_progress if progress_callback else None,
        )
        if progress_callback:
            progress_callback(value=100, text="订阅刷新完成")

    def check(
        self,
        progress_callback: Optional[Callable[..., None]] = None,
        reconcile_completion: bool = False,
    ) -> None:
        """
        定时检查订阅，更新订阅信息

        :param progress_callback: 定时服务进度更新回调
        :param reconcile_completion: 是否复用本次新鲜媒体事实执行独立完成对账
        """
        # 查询所有订阅
        repository = self.subscription_repository
        subscribes = repository.list()
        fresh_fact_lease = FreshFactLease()
        total_num = len(subscribes)
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始更新订阅元数据，共 {total_num} 个订阅 ...",
                data={"total": total_num, "finished": 0},
            )
        # 遍历订阅
        for index, subscribe in enumerate(subscribes, start=1):
            if runtime_stop_state.is_system_stopped:
                break
            logger.info(f"开始更新订阅元数据：{subscribe.name} ...")
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_num * 100 if total_num else 100,
                    text=f"正在更新订阅元数据（{index}/{total_num}）{subscribe.name} ...",
                    data={
                        "total": total_num,
                        "finished": index - 1,
                        "current": subscribe.id,
                    },
                )
            updated_subscribe = self._check_subscription(
                subscribe,
                fresh_fact_lease,
                reconcile_completion=reconcile_completion,
                media_chain_factory=MediaChain,
            )
            if updated_subscribe is None:
                continue
            logger.info(f"{updated_subscribe.name} 订阅元数据更新完成")
            if progress_callback:
                progress_callback(
                    value=index / total_num * 100 if total_num else 100,
                    text=f"订阅元数据（{index}/{total_num}）更新完成",
                    data={"total": total_num, "finished": index},
                )
        if progress_callback:
            progress_callback(value=100, text="订阅元数据更新完成")

    def check_and_reconcile(
        self,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """刷新订阅元数据，并复用同一轮新鲜事实执行完成对账。"""
        return self.check(
            progress_callback=progress_callback,
            reconcile_completion=True,
        )

    async def cache_calendar(
        self,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        预缓存订阅日历，实际上就是查询一遍所有订阅的媒体信息
        前端请示是异常的，所以需要使用异步缓存方法

        :param progress_callback: 定时服务进度更新回调
        """
        logger.info("开始预缓存订阅日历 ...")
        subscribes = await self.subscription_repository.async_list()
        total_num = len(subscribes)
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始预缓存订阅日历，共 {total_num} 个订阅 ...",
                data={"total": total_num, "finished": 0},
            )
        for index, subscribe in enumerate(subscribes, start=1):
            if runtime_stop_state.is_system_stopped:
                break
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_num * 100 if total_num else 100,
                    text=f"正在预缓存订阅日历（{index}/{total_num}）{subscribe.name} ...",
                    data={
                        "total": total_num,
                        "finished": index - 1,
                        "current": subscribe.id,
                    },
                )
            try:
                mtype = MediaType(subscribe.type)
            except ValueError:
                logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}")
                continue
            # 先按订阅的主媒体身份预热对应数据源，再对 TMDB 额外预热分集接口。
            if mtype == MediaType.MUSIC:
                mediainfo = await self._async_recognize_music_subscribe(subscribe)
            else:
                mediainfo: MediaInfo = await MediaChain().async_recognize_media(
                    mtype=mtype,
                    **subscribe_recognize_kwargs(subscribe),
                    episode_group=subscribe.episode_group,
                    cache=False,
                )
            if not mediainfo:
                logger.warn(
                    f"未识别到媒体信息，标题：{subscribe.name}，"
                    f"媒体源：{subscribe.media_source}，媒体ID：{subscribe.media_id}"
                )
                continue
            if mtype == MediaType.TV and mediainfo.media_source == MediaSource.TMDB and mediainfo.tmdb_id:
                episodes = await TmdbChain().async_tmdb_episodes(
                    tmdbid=mediainfo.tmdb_id, season=subscribe.season, episode_group=subscribe.episode_group
                )
                if not episodes:
                    logger.warn(
                        f"未识别到季集信息，标题：{subscribe.name}，tmdbid：{mediainfo.tmdb_id}，季：{subscribe.season}"
                    )
                    continue
            if progress_callback:
                progress_callback(
                    value=index / total_num * 100 if total_num else 100,
                    text=f"订阅日历（{index}/{total_num}）预缓存完成",
                    data={"total": total_num, "finished": index},
                )
        logger.info("订阅日历预缓存完成")
        if progress_callback:
            progress_callback(value=100, text="订阅日历预缓存完成")

    def resolve_subscribe_missing(
        self,
        subscribe: SubscriptionSnapshot,
        meta: MetaBase,
        mediainfo: MediaInfo,
        mediakey: Optional[Union[str, int]] = None,
        best_version_accept_downloaded: bool = False,
    ) -> Tuple[bool, Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]]:
        """
        按主程序订阅口径查询当前目标是否仍有缺失，不推进订阅状态。

        该方法只组合媒体库缺集、订阅范围、下载历史和洗版优先级，用于外部策略在
        完成前复用主程序"还要不要搜索/下载"的判断口径。它不得完成订阅、写入
        lack_episode、发送事件或修改数据库。

        best_version_accept_downloaded 仅用于分集洗版的外部完成守卫：为 True 时，
        priority>0 的目标集视为已满足；默认 False 保持主程序洗版完成需 priority==100
        的搜索/完成口径。
        """
        mediakey = mediakey or subscribe_media_key(subscribe)
        effective_total_episode = self._SubscribeChain__resolve_effective_total_episode(subscribe, mediainfo)

        if not subscribe.best_version:
            totals = {}
            if subscribe.season is not None and effective_total_episode:
                totals = {subscribe.season: effective_total_episode}
            exist_flag, no_exists = DownloadChain().get_no_exists_info(meta=meta, mediainfo=mediainfo, totals=totals)
        elif meta.type != MediaType.TV and self._SubscribeChain__is_best_version_complete(subscribe):
            return True, {}
        else:
            exist_flag = False
            if meta.type == MediaType.TV:
                if self._SubscribeChain__is_full_best_version_enabled(subscribe):
                    pending_episodes = []
                elif best_version_accept_downloaded:
                    downloaded_set = set(
                        self._SubscribeChain__get_downloaded_best_version_episodes(
                            subscribe, total_episode=effective_total_episode
                        )
                    )
                    start_episode = subscribe.start_episode or 1
                    pending_episodes = [
                        episode
                        for episode in range(start_episode, effective_total_episode + 1)
                        if episode not in downloaded_set
                    ]
                    if not pending_episodes:
                        return True, {}
                else:
                    pending_episodes = self._get_pending_best_version_episodes(
                        subscribe, total_episode=effective_total_episode
                    )
                    if not pending_episodes:
                        return True, {}
                no_exists = {
                    mediakey: {
                        subscribe.season: _SchemaNotExistMediaInfo(
                            season=subscribe.season,
                            episodes=pending_episodes,
                            total_episode=effective_total_episode,
                            start_episode=subscribe.start_episode or 1,
                            require_complete_coverage=self._SubscribeChain__is_full_best_version_enabled(subscribe),
                        )
                    }
                }
            else:
                no_exists = {}

        if exist_flag:
            return True, no_exists

        downloaded: List[int] = self._SubscribeChain__get_downloaded(subscribe)
        if self._SubscribeChain__is_full_best_version_enabled(subscribe):
            downloaded = []
        if meta.type == MediaType.TV:
            return cast(
                Tuple[bool, Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]],
                self._SubscribeChain__get_subscribe_no_exits(
                    subscribe_name=f"{subscribe.name} {meta.season}",
                    no_exists=no_exists,
                    mediakey=mediakey,
                    begin_season=meta.begin_season,
                    total_episode=effective_total_episode,
                    start_episode=subscribe.start_episode,
                    downloaded_episodes=downloaded,
                ),
            )
        if meta.type in (MediaType.MOVIE, MediaType.MUSIC):
            return bool(downloaded), no_exists
        return False, no_exists

    def _SubscribeChain__resolve_total_episode_decrease(
        self,
        subscribe: SubscriptionSnapshot,
        candidate_total: int,
        meta: MetaBase,
        mediainfo: MediaInfo,
        mediakey: Optional[Union[str, int]] = None,
    ) -> int:
        """以旧目标范围内已确认存在的最高集号限制总集数回落。"""
        old_total = subscribe.total_episode or 0
        if candidate_total >= old_total or not old_total:
            return candidate_total
        if subscribe.type != MediaType.TV.value or self._SubscribeChain__is_full_best_version_enabled(subscribe):
            return candidate_total

        target_key = mediakey or subscribe_media_key(subscribe)
        target_season = subscribe.season
        target_start = subscribe.start_episode or 1
        snapshot = replace(subscribe, total_episode=old_total)
        try:
            satisfied, no_exists = self.resolve_subscribe_missing(
                subscribe=snapshot,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=target_key,
                best_version_accept_downloaded=bool(subscribe.best_version),
            )
        except Exception as err:
            logger.warning(f"订阅 {subscribe.name} 已存在分集事实查询失败，按元数据总集数继续：{err}")
            return candidate_total

        if satisfied:
            return old_total
        if not isinstance(no_exists, dict):
            return candidate_total
        seasons = next(
            (
                no_exists.get(media_key)
                for media_key in [target_key, *subscribe_media_keys(subscribe)]
                if no_exists.get(media_key) is not None
            ),
            None,
        )
        if not isinstance(seasons, dict):
            return candidate_total
        missing_info = seasons.get(target_season)
        if not missing_info:
            return candidate_total
        try:
            scope_matches = (
                missing_info.season == target_season
                and missing_info.start_episode == target_start
                and missing_info.total_episode == old_total
            )
            episodes = missing_info.episodes
        except AttributeError:
            return candidate_total
        if not scope_matches:
            return candidate_total
        if not isinstance(episodes, list) or not episodes:
            return candidate_total
        if any(
            isinstance(episode, bool) or not isinstance(episode, int) or episode < target_start or episode > old_total
            for episode in episodes
        ):
            return candidate_total

        confirmed = set(range(target_start, old_total + 1)).difference(episodes)
        return max(candidate_total, max(confirmed) if confirmed else 0)

    @staticmethod
    def _SubscribeChain__resolve_effective_total_episode(subscribe: SubscriptionSnapshot, mediainfo: MediaInfo) -> int:
        """
        只读计算完成前有效总集数，不触发事件、不写回订阅。

        主流程会通过 ``__refresh_total_episode_before_completion`` 持久化增长后的总集数；
        该查询接口只需要同样避免旧 total 造成误判，因此仅使用当前 mediainfo 中更大的
        季集数作为临时目标范围。
        """
        current_total = subscribe.total_episode or 0
        if subscribe.type != MediaType.TV.value:
            return current_total
        if subscribe.manual_total_episode:
            return current_total
        if subscribe.season is None:
            return current_total
        media_total = len((mediainfo.seasons or {}).get(subscribe.season) or [])
        if media_total > current_total:
            return media_total
        return current_total

    @staticmethod
    def _SubscribeChain__apply_episodes_refresh(
        current_total: int,
        season: Optional[int],
        *,
        mediainfo: Optional[MediaInfo] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        subscribe_id: Optional[int] = None,
        scene: Optional[str] = None,
    ) -> int:
        """
        发送订阅总集数推算事件，允许外部把当前数据源识别到的季总集数向上覆盖。

        用途：插件在"待定集数"等场景经事件注入 total_episode
        无监听者或外部未覆盖时返回入参原值，保证零行为变更。
        :param current_total: 主程序本次识别到的当前季总集数
        :param season: 季号
        :return: 最终采用的总集数
        """
        event_data = SubscribeEpisodesRefreshEventData(
            media_source=media_source,
            media_id=media_id,
            season=season,
            mediainfo=mediainfo,
            current_total_episode=current_total,
            subscribe_id=subscribe_id,
            scene=scene,
        )
        event = eventmanager.send_event(ChainEventType.SubscribeEpisodesRefresh, event_data)
        if event and event.event_data:
            result: SubscribeEpisodesRefreshEventData = event.event_data
            if result.updated and result.total_episode:
                result.total_episode = max(current_total or 0, result.total_episode)
                total_episode: int = result.total_episode
                return total_episode
        return current_total

    @staticmethod
    async def _SubscribeChain__async_apply_episodes_refresh(
        current_total: int,
        season: Optional[int],
        *,
        mediainfo: Optional[MediaInfo] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        subscribe_id: Optional[int] = None,
        scene: Optional[str] = None,
    ) -> int:
        """
        __apply_episodes_refresh 的异步版本
        """
        event_data = SubscribeEpisodesRefreshEventData(
            media_source=media_source,
            media_id=media_id,
            season=season,
            mediainfo=mediainfo,
            current_total_episode=current_total,
            subscribe_id=subscribe_id,
            scene=scene,
        )
        event = await eventmanager.async_send_event(ChainEventType.SubscribeEpisodesRefresh, event_data)
        if event and event.event_data:
            result: SubscribeEpisodesRefreshEventData = event.event_data
            if result.updated and result.total_episode:
                result.total_episode = max(current_total or 0, result.total_episode)
                total_episode: int = result.total_episode
                return total_episode
        return current_total

    def _SubscribeChain__refresh_total_episode_before_completion(
        self,
        subscribe: SubscriptionSnapshot,
        mediainfo: MediaInfo,
        meta: Optional[MetaBase] = None,
        mediakey: Optional[Union[str, int]] = None,
    ) -> SubscriptionSnapshot:
        """
        在完成判断前，按最新识别结果兜底修正订阅总集数，防止旧总集数导致误完成。
        """
        if subscribe.type != MediaType.TV.value:
            return subscribe
        if subscribe.manual_total_episode:
            return subscribe
        if subscribe.season is None:
            return subscribe

        current_total_episode = len((mediainfo.seasons or {}).get(subscribe.season) or [])
        # 外部事件只能向上覆盖主程序本次识别到的 TMDB 当前季总集数，已有订阅回落由主程序跟随本次识别结果持久化。
        new_total_episode = self._SubscribeChain__apply_episodes_refresh(
            current_total_episode,
            season=subscribe.season,
            mediainfo=mediainfo,
            media_source=subscribe.media_source,
            media_id=subscribe.media_id,
            subscribe_id=subscribe.id,
            scene="precheck",
        )
        old_total_episode = subscribe.total_episode or 0
        if meta is not None and new_total_episode and new_total_episode < old_total_episode:
            new_total_episode = self._SubscribeChain__resolve_total_episode_decrease(
                subscribe=subscribe,
                candidate_total=new_total_episode,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=mediakey,
            )
        if not new_total_episode or new_total_episode == old_total_episode:
            return subscribe

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_data = self._SubscribeChain__prepare_total_episode_change_fields(
            subscribe=subscribe,
            total_episode=new_total_episode,
            old_total_episode=old_total_episode,
        )
        update_data["last_update"] = now
        subscribe = self._SubscribeChain__apply_subscribe_update(
            subscribe,
            update_data,
            scene="episode_refresh",
        )
        logger.info(
            f"订阅 {subscribe.name} 第{subscribe.season}季 总集数更新为 {new_total_episode}，"
            f"缺失集数更新为 {subscribe.lack_episode}"
        )
        return subscribe

    @classmethod
    def _SubscribeChain__prepare_subscribe_progress_fields(
        cls,
        subscribe: SubscriptionSnapshot,
        no_exists: Optional[Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]] = None,
        touch_last_update: Optional[bool] = False,
    ) -> Dict[str, Any]:
        """准备订阅进度持久化字段；算法见 application.subscription.priority。"""
        return cast(
            Dict[str, Any],
            _priority.prepare_subscribe_progress_fields(
                subscribe=subscribe,
                no_exists=no_exists,
                touch_last_update=touch_last_update,
            ),
        )

    def _SubscribeChain__refresh_subscribe_progress_with_no_exists(
        self,
        subscribe: SubscriptionSnapshot,
        no_exists: Optional[Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]] = None,
        touch_last_update: Optional[bool] = False,
        scene: str = "download",
    ) -> Dict[str, Any]:
        """
        使用已解析的缺失信息刷新订阅进度，避免下载链路重复查询媒体库。
        """
        old_lack_episode = subscribe.lack_episode
        old_current_priority = subscribe.current_priority
        update_data = self._SubscribeChain__prepare_subscribe_progress_fields(
            subscribe=subscribe,
            no_exists=no_exists,
            touch_last_update=touch_last_update,
        )
        if not update_data:
            return {"scene": scene, "updated": False, "fields": [], "reason": "unsupported_subscribe_type"}

        subscribe = self._SubscribeChain__apply_subscribe_update(subscribe, update_data)
        logger.info(
            f"订阅 {subscribe.id} 进度刷新：scene={scene}，fields={list(update_data)}，"
            f"lack_episode {old_lack_episode}->{subscribe.lack_episode}，"
            f"current_priority {old_current_priority}->{subscribe.current_priority}，reason=updated"
        )
        return {
            "scene": scene,
            "updated": True,
            "fields": list(update_data),
            "lack_episode": update_data.get("lack_episode", subscribe.lack_episode),
            "current_priority": update_data.get("current_priority", subscribe.current_priority),
            "reason": "updated",
            "subscribe": subscribe,
        }

    def refresh_subscribe_progress(self, subscribe: SubscriptionSnapshot, *, scene: str = "update") -> Dict[str, Any]:
        """
        按主程序口径重新计算并持久化订阅进度。
        """
        if subscribe.type != MediaType.TV.value:
            return {"scene": scene, "updated": False, "fields": [], "reason": "unsupported_subscribe_type"}

        no_exists = None
        mediainfo = None
        if not subscribe.best_version:
            meta = build_subscribe_meta(subscribe)
            mediainfo = MediaChain().recognize_media(
                meta=meta,
                mtype=meta.type,
                **subscribe_recognize_kwargs(subscribe),
                episode_group=subscribe.episode_group,
                cache=False,
            )
            if not mediainfo:
                return {"scene": scene, "updated": False, "fields": [], "reason": "recognize_failed"}
            mediakey = subscribe_media_key(subscribe)
            exist_flag, no_exists = self.resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=mediakey,
            )
            if not exist_flag and not no_exists:
                return {"scene": scene, "updated": False, "fields": [], "reason": "resolve_missing_failed"}

        return self._SubscribeChain__refresh_subscribe_progress_with_no_exists(
            subscribe=subscribe,
            no_exists=no_exists,
            touch_last_update=False,
            scene=scene,
        )

    def backfill_existing_episodes(
        self,
        subscribe: SubscriptionSnapshot,
        episodes: List[Union[int, str]],
        priority: Optional[int] = None,
        scene: str = "backfill",
    ) -> Dict[str, Any]:
        """
        将媒体库既有剧集补写为订阅下载事实，并按需刷新进度字段。
        """
        accepted = []
        ignored = []
        priority_updated = []
        priority_ignored = []
        target_episodes = set(self._SubscribeChain__get_best_version_target_episodes(subscribe))
        note = sorted({int(episode) for episode in subscribe.note or [] if str(episode).isdigit()})
        note_set = set(note)
        priority_episodes = set()

        for episode in episodes or []:
            try:
                episode_number = int(episode)
            except (TypeError, ValueError):
                ignored.append({"episode": episode, "reason": "invalid"})
                continue
            if episode_number not in target_episodes:
                ignored.append({"episode": episode, "reason": "out_of_range"})
                continue
            if episode_number in note_set:
                ignored.append({"episode": episode, "reason": "duplicate"})
                priority_episodes.add(episode_number)
                continue
            accepted.append(episode_number)
            note_set.add(episode_number)
            priority_episodes.add(episode_number)

        summary: Dict[str, Any] = {
            "scene": scene,
            "accepted": accepted,
            "ignored": ignored,
            "priority_updated": priority_updated,
            "priority_ignored": priority_ignored,
            "fields": [],
        }
        update_data: Dict[str, Any] = {}
        if accepted:
            note = sorted(note_set)
            update_data["note"] = note

        priority_valid = isinstance(priority, int) and not isinstance(priority, bool) and 1 <= priority <= 100
        if priority is not None and not priority_valid:
            summary["ignored_priority"] = priority
        if priority_valid:
            episode_priority = self._SubscribeChain__get_episode_priority(subscribe)
            for episode_number in sorted(priority_episodes):
                episode_key = str(episode_number)
                old_priority = episode_priority.get(episode_key)
                if old_priority is None or priority > old_priority:
                    episode_priority[episode_key] = priority
                    priority_updated.append(episode_number)
                else:
                    priority_ignored.append(
                        {
                            "episode": episode_number,
                            "reason": "duplicate" if old_priority == priority else "not_higher_priority",
                        }
                    )
            if priority_updated:
                update_data["episode_priority"] = episode_priority

        should_refresh_progress = subscribe.type == MediaType.TV.value and (accepted or priority_updated)
        progress_summary = None
        if should_refresh_progress and subscribe.best_version:
            working_snapshot = replace(subscribe, **update_data)
            update_data.update(
                self._SubscribeChain__prepare_subscribe_progress_fields(
                    subscribe=working_snapshot,
                    touch_last_update=True,
                )
            )

        if update_data:
            subscribe = self._SubscribeChain__apply_subscribe_update(subscribe, update_data)
            summary["fields"] = list(update_data)
        if should_refresh_progress and not subscribe.best_version:
            progress_summary = self.refresh_subscribe_progress(subscribe, scene=scene)
        if progress_summary is not None:
            summary["progress"] = progress_summary
            subscribe = progress_summary.get("subscribe", subscribe)
        summary["subscribe"] = subscribe
        summary["updated"] = bool(update_data)
        if progress_summary:
            summary["updated"] = summary["updated"] or bool(progress_summary.get("updated"))
        return summary
