"""订阅下载事实、缺失集与完成状态收敛"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from app.application.subscription.contract import (
    SubscriptionSnapshot,
    subscribe_media_key,
    subscribe_media_keys,
)
from app.application.subscription.mutation import SubscriptionActor
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.policy import SubscribePolicyOwner
from app.domain.context import (
    Context,
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.event import SubscribeCompletionCheckEventData
from app.schemas.media import resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    ChainEventType,
    MediaType,
)


class SubscribeCompletionOwner(_SubscribeOwnerBase):
    """订阅下载事实、缺失集与完成状态收敛，作为 SubscribeChain 的单一职责实现 owner。"""

    def _SubscribeChain__update_movie_download_priority(
        self,
        subscribe: SubscriptionSnapshot,
        mediainfo: MediaInfo,
        downloads: Optional[List[Context]],
    ) -> SubscriptionSnapshot:
        """
        记录电影本轮下载资源优先级，用作后续电影洗版的起始质量状态。
        """
        if not downloads:
            return subscribe
        priority = max([item.torrent_info.pri_order for item in downloads])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if subscribe.type != MediaType.MOVIE.value:
            return subscribe

        updated = self._SubscribeChain__apply_subscribe_update(
            subscribe,
            {"current_priority": priority, "last_update": now},
            scene="movie_download",
        )
        if subscribe.best_version and priority != 100:
            # 正在洗版，更新资源优先级
            logger.info(f"{mediainfo.title_year} 正在洗版，更新资源优先级为 {priority}")
        return updated or subscribe

    def _SubscribeChain__finish_subscribe(
        self,
        subscribe: SubscriptionSnapshot,
        mediainfo: MediaInfo,
        meta: MetaBase,
    ) -> None:
        """完成订阅事务，并在提交成功后委托通知 owner 投递副作用。"""
        if subscribe.state == "P":
            return
        completion_event = self.eventmanager.send_event(
            ChainEventType.SubscribeCompletionCheck,
            SubscribeCompletionCheckEventData(
                subscribe=subscribe,
                mediainfo=mediainfo,
                meta=meta,
            ),
        )
        if completion_event and completion_event.event_data:
            completion_data: SubscribeCompletionCheckEventData = completion_event.event_data
            if completion_data.cancel:
                logger.info(f"{mediainfo.title_year} 完成被 [{completion_data.source}] 否决：{completion_data.reason}")
                return

        msgstr = "订阅" if not subscribe.best_version else "洗版"
        logger.info(f"{mediainfo.title_year} 完成{msgstr}")
        completion_message = self._SubscribeChain__build_completion_notification(
            subscribe=subscribe,
            mediainfo=mediainfo,
            meta=meta,
        )

        def notify() -> None:
            """提交成功后发送完成通知，保持历史消息 ABI。"""
            self.post_message(completion_message)

        with self.subscription_completion_scope() as command:
            command.execute(
                subscribe_id=subscribe.id,
                subscribe_info=subscribe.to_dict(),
                mediainfo=mediainfo.to_dict(),
                notify=notify,
                report=self._SubscribeChain__report_completed,
                notification=completion_message.model_dump(mode="json"),
            )

    def finish_subscribe_or_not(
        self,
        subscribe: SubscriptionSnapshot,
        meta: MetaBase,
        mediainfo: MediaInfo,
        downloads: List[Context] = None,
        lefts: Dict[Union[int | str], Dict[int, _SchemaNotExistMediaInfo]] = None,
        force: Optional[bool] = False,
    ) -> None:
        """
        判断是否应完成订阅
        """
        media_keys = subscribe_media_keys(subscribe)
        # 是否有剩余集
        no_lefts = not lefts or not any(lefts.get(media_key) for media_key in media_keys)
        if downloads and meta.type == MediaType.TV:
            facts = self._SubscribeChain__record_subscribe_download_facts(
                subscribe=subscribe,
                mediainfo=mediainfo,
                downloads=downloads,
            )
            subscribe = facts["subscribe"]
        elif downloads:
            self._SubscribeChain__update_subscribe_note(subscribe=subscribe, downloads=downloads)
        if downloads and meta.type == MediaType.MOVIE:
            subscribe = self._SubscribeChain__update_movie_download_priority(
                subscribe=subscribe,
                mediainfo=mediainfo,
                downloads=downloads,
            )
        # 是否完成订阅
        if not subscribe.best_version:
            # 普通订阅：先按 lefts 写 lack，再判断完成
            if meta.type == MediaType.TV:
                self._SubscribeChain__refresh_subscribe_progress_with_no_exists(
                    no_exists=lefts,
                    subscribe=subscribe,
                    touch_last_update=bool(downloads),
                    scene="download",
                )
            if (
                (no_lefts and meta.type == MediaType.TV)
                or (downloads and meta.type == MediaType.MOVIE)
                or (meta.type == MediaType.MUSIC and self._is_music_download_complete(subscribe, mediainfo, downloads))
                or force
            ):
                self._SubscribeChain__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
            else:
                logger.info(f"{mediainfo.title_year} 未下载完整，继续订阅 ...")
            return

        if meta.type == MediaType.TV:
            self._SubscribeChain__refresh_subscribe_progress_with_no_exists(
                no_exists=lefts,
                subscribe=subscribe,
                touch_last_update=bool(downloads),
                scene="download",
            )
        if meta.type == MediaType.MUSIC and not self._is_music_download_complete(
            subscribe,
            mediainfo,
            downloads,
        ):
            logger.info(f"{mediainfo.title_year} 未下载完整，继续洗版 ...")
            return
        if self._SubscribeChain__is_best_version_complete(subscribe):
            # 洗版完成
            self._SubscribeChain__finish_subscribe(subscribe=subscribe, meta=meta, mediainfo=mediainfo)
        elif not downloads:
            logger.info(f"{mediainfo.title_year} 继续洗版 ...")

    def reconcile_subscription_completion(
        self,
        subscribe: SubscriptionSnapshot,
        meta: MetaBase,
        mediainfo: MediaInfo,
    ) -> bool:
        """使用已取得的新鲜媒体事实独立对账订阅完成状态。"""
        mediakey = subscribe_media_key(subscribe)
        completed, _no_exists = self.check_and_handle_existing_media(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=mediakey,
        )
        return bool(completed)

    def _SubscribeChain__update_subscribe_note(
        self,
        subscribe: SubscriptionSnapshot,
        downloads: Optional[List[Context]],
    ) -> None:
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
            subscribe_source, subscribe_media_id = resolve_media_identity(media=subscribe)
            media_source, media_id = resolve_media_identity(media=mediainfo)
            if (
                subscribe_source != media_source
                or not subscribe_media_id
                or not media_id
                or subscribe_media_id != media_id
            ):
                continue
            items = []
            if mediainfo.type == MediaType.TV:
                # 电视剧有集数，使用 episode_list
                items = meta.episode_list
            elif mediainfo.type == MediaType.MOVIE:
                # 电影只有一个条目，设置为 [1]
                items = [1]
            elif mediainfo.type == MediaType.MUSIC:
                # 专辑只能记录已由下载层确认的整专资源；单曲任一成功任务即可完成。
                if getattr(subscribe, "music_type", None) != MUSIC_ENTITY_ALBUM or context.confirmed_full_coverage:
                    items = [1]
            if not items:
                continue
            # 合并已下载的集数或电影项（去重）
            note = list(set(note).union(set(items)))
        # 更新订阅
        if note:
            self._SubscribeChain__apply_subscribe_update(
                subscribe,
                {"note": note},
                scene="download_note",
            )

    @staticmethod
    def _SubscribeChain__get_downloaded(subscribe: SubscriptionSnapshot) -> List[int]:
        """
        获取已下载过的集数或电影。

        洗版分支只返回 priority==100 的完成集；priority<100 的集仍要继续搜索更高
        优先级版本，不能并入返回值（会让下游把 pending 减空、订阅卡死）。
        note 由非洗版分支消费，用于洗版关闭后的迁移读取。
        """
        if subscribe.best_version:
            if subscribe.type == MediaType.TV.value:
                completed = SubscribePolicyOwner._SubscribeChain__get_best_version_completed_episodes(subscribe)
                if completed:
                    logger.info(f"订阅 {subscribe.name} 第{subscribe.season}季 已完成洗版剧集：{completed}")
                return completed
            return []
        note = subscribe.note or []
        if not note:
            return []
        # 针对 TV 类型，返回已下载的集数
        if subscribe.type == MediaType.TV.value:
            logger.info(f"订阅 {subscribe.name} 第{subscribe.season}季 已下载集数：{note}")
            return note
        # 针对 Movie/Music 类型，直接返回已下载的单项内容
        if subscribe.type in (MediaType.MOVIE.value, MediaType.MUSIC.value):
            logger.info(f"订阅 {subscribe.name} 已下载内容：{note}")
            return note
        return []

    def _SubscribeChain__apply_subscribe_update(
        self,
        subscribe: SubscriptionSnapshot,
        update_data: Mapping[str, JsonData],
        *,
        scene: str = "progress",
    ) -> SubscriptionSnapshot:
        """
        写入订阅字段并同步当前内存对象，保证后续事件和判断读取最终快照。
        """
        if not update_data:
            return subscribe
        with self.sync_subscription_mutation_scope() as mutation:
            change = mutation.update(
                subscribe.id,
                dict(update_data),
                SubscriptionActor(name="chain", is_superuser=True),
                existing=subscribe,
                scene=scene,
            )
        return change.snapshot if change else subscribe

    def _SubscribeChain__record_subscribe_download_facts(
        self,
        subscribe: SubscriptionSnapshot,
        *,
        mediainfo: MediaInfo,
        downloads: Optional[List[Context]],
    ) -> Dict[str, Any]:
        """
        记录主程序本轮下载产生的订阅事实，并返回本轮覆盖摘要。
        """
        if not downloads:
            return {
                "episodes": [],
                "fields": [],
                "updated": False,
                "subscribe": subscribe,
            }

        covered_episodes = set()
        written_priorities: Dict[str, int] = {}
        used_full_coverage_fallback = False
        episode_priority = self._SubscribeChain__get_episode_priority(subscribe)
        note_set = {int(episode) for episode in subscribe.note or [] if str(episode).isdigit()}
        update_data: Dict[str, Any] = {}

        for download in downloads:
            media = download.media_info
            subscribe_identity = resolve_media_identity(media=subscribe)
            media_identity = resolve_media_identity(media=media)
            if subscribe_identity != media_identity:
                continue

            if subscribe.type == MediaType.MOVIE.value and media.type == MediaType.MOVIE:
                note_set.add(1)
                covered_episodes.add(1)
                continue

            if subscribe.type != MediaType.TV.value or media.type != MediaType.TV:
                continue

            selected_episodes = getattr(download, "selected_episodes", None)
            if selected_episodes:
                episodes = selected_episodes
            elif download.meta_info and download.meta_info.episode_list:
                episodes = download.meta_info.episode_list
            elif download.confirmed_full_coverage:
                episodes = self._SubscribeChain__get_best_version_target_episodes(subscribe)
                used_full_coverage_fallback = True
            else:
                episodes = []

            valid_episodes = []
            for episode in episodes:
                try:
                    episode_number = int(episode)
                except (TypeError, ValueError):
                    continue
                if episode_number not in self._SubscribeChain__get_best_version_target_episodes(subscribe):
                    continue
                valid_episodes.append(episode_number)
            if not valid_episodes:
                continue

            priority = download.torrent_info.pri_order
            if (
                self._SubscribeChain__is_full_best_version_enabled(subscribe)
                and download.confirmed_full_coverage
                and isinstance(priority, int)
                and not isinstance(priority, bool)
                and priority > (subscribe.current_priority or 0)
            ):
                update_data["current_priority"] = priority
            for episode_number in valid_episodes:
                note_set.add(episode_number)
                covered_episodes.add(episode_number)
                episode_key = str(episode_number)
                old_priority = episode_priority.get(episode_key)
                if (
                    isinstance(priority, int)
                    and not isinstance(priority, bool)
                    and (old_priority is None or priority > old_priority)
                ):
                    episode_priority[episode_key] = priority
                    written_priorities[episode_key] = priority

        if covered_episodes:
            update_data["note"] = sorted(note_set)
            if subscribe.type == MediaType.TV.value:
                update_data["episode_priority"] = episode_priority
            update_data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if update_data:
            subscribe = self._SubscribeChain__apply_subscribe_update(subscribe, update_data)
            logger.info(
                f"{mediainfo.title_year} 订阅 {subscribe.id} 第 {subscribe.season} 季记录下载事实："
                f"mode=best_version:{subscribe.best_version},full:{subscribe.best_version_full}，"
                f"covered_episodes={sorted(covered_episodes)}，episode_priority={written_priorities}，"
                f"confirmed_full_coverage_fallback={used_full_coverage_fallback}"
            )
        return {
            "episodes": sorted(covered_episodes),
            "fields": list(update_data),
            "updated": bool(update_data),
            "subscribe": subscribe,
        }

    @staticmethod
    def _SubscribeChain__get_subscribe_no_exits(
        subscribe_name: str,
        no_exists: Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]],
        mediakey: Union[str, int],
        begin_season: int,
        total_episode: Optional[int],
        start_episode: Optional[int],
        downloaded_episodes: List[int] = None,
    ) -> Tuple[bool, Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]]:
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
            logger.info(f"订阅 {subscribe_name} 设定的开始集数：{start_episode}、总集数：{total_episode}")
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
                    start_episode = start_episode or start
                    total_episode = total_episode or total
                    original_start = start if start is not None else 1
                    # 空集列表会被下载链解释为整季下载；当订阅开始集裁掉季初范围时，需要转成显式集数。
                    if start_episode and total_episode and start_episode > original_start:
                        episodes = list(range(start_episode, total_episode + 1))
                        if not episodes:
                            return True, {}
                    else:
                        episodes = []
                else:
                    # 部分缺失
                    if not start_episode and not total_episode:
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
                no_exists[mediakey][begin_season] = _SchemaNotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start_episode,
                    require_complete_coverage=no_exist_season.require_complete_coverage,
                )
        # 根据订阅已下载集数更新缺失集数
        if downloaded_episodes:
            logger.info(f"订阅 {subscribe_name} 已下载集数：{downloaded_episodes}")
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
                no_exists[mediakey][begin_season] = _SchemaNotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total,
                    start_episode=start,
                    require_complete_coverage=no_exist_season.require_complete_coverage,
                )
            else:
                # 开始集数
                start = start_episode or 1
                # 更新剧集列表
                episodes = list(set(range(start, total_episode + 1)).difference(set(downloaded_episodes)))
                # 如果存在已下载剧集，则差集为空时，说明所有均已存在
                if not episodes:
                    return True, {}
                no_exists[mediakey][begin_season] = _SchemaNotExistMediaInfo(
                    season=begin_season,
                    episodes=episodes,
                    total_episode=total_episode,
                    start_episode=start,
                    require_complete_coverage=False,
                )
        logger.info(f"订阅 {subscribe_name} 缺失剧集数更新为：{no_exists}")
        return False, no_exists
